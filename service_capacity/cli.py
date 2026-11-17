from __future__ import annotations
import argparse
import datetime as dt
import decimal
import json
from enum import Enum
from pathlib import Path
from .reporting import Reporter
from .service import ApplicationService
from .storage import Database
from .demand_planning import DemandPlanningService

def load_json(path:str):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def _json_default(value):
    if isinstance(value,(dt.date,dt.datetime)):
        return value.isoformat()
    if isinstance(value,decimal.Decimal):
        text=format(value,'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    if isinstance(value,Enum):
        return value.value
    raise TypeError(type(value).__name__)

def _default(*values, default=None):
    for value in values:
        if value is not None:
            return value
    return default

def _planning_payload(fixture:dict):
    """Extract a planning request from fixture shapes used by the CLI.

    Accepted top-level keys: ``as_of``/``decision_date``/``cutoff``,
    ``observations`` (or ``history``), ``capacity``/``roster``/``staffing``,
    ``calendar``, ``horizon``, and constraint overrides.  A list under
    ``items`` may carry the same keys per item; each item is planned
    independently.
    """

    context=dict(fixture.get('context') or {})
    as_of=_default(fixture.get('as_of'),fixture.get('decision_date'),fixture.get('cutoff'),context.get('as_of'),context.get('decision_date'),context.get('cutoff'))
    observations=_default(fixture.get('observations'),fixture.get('history'),context.get('observations'))
    capacity=_default(fixture.get('capacity'),fixture.get('roster'),fixture.get('staffing'),context.get('capacity_agents'),context.get('roster'))
    calendar=_default(fixture.get('calendar'),context.get('calendar'),default={})
    horizon=int(_default(fixture.get('horizon'),context.get('horizon'),default=1))
    constraints={key:value for key,value in _default(fixture.get('constraints'),{},).items()}
    for key in ('service_minutes','shift_minutes','occupancy_target','overload_threshold','maximum_agents'):
        if key in fixture: constraints[key]=fixture[key]
        elif key in context: constraints[key]=context[key]
    options={}
    for key in ('confidence','min_factor','max_factor','minimum_history','min_samples','train_window','strict_point_in_time','site_id','service_type'):
        if key in fixture: options[key]=fixture[key]
        elif key in context: options[key]=context[key]
    adjustments=_default(fixture.get('adjustments'),context.get('adjustments'))
    return {'as_of':as_of,'observations':observations,'capacity':capacity,'calendar':calendar,'horizon':horizon,'constraints':constraints,'options':options,'adjustments':adjustments}

def _plan_one(service:DemandPlanningService,request:dict):
    calibration=service.calibrate(request['observations'],request['as_of'],horizon=request['horizon'],calendar=request['calendar'],**request['options'])
    planner=service.planner
    constraints=request.get('constraints') or {}
    if constraints:
        from .demand_planning import CapacityPlanner,StaffingConstraints
        planner=CapacityPlanner(StaffingConstraints(**constraints))
    plan=planner.plan(calibration,capacity=request['capacity'],calendar=request['calendar'])
    service.ledger.record_plan(plan,calibration)
    result={'calibration':calibration,'plan':plan}
    if request.get('adjustments') is not None:
        revised=service.revise(plan,request['adjustments'])
        result['revision']=revised
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(prog='service_capacity')
    parser.add_argument('--database',default=':memory:')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run'); run.add_argument('--fixture',required=True); run.add_argument('--output',required=True)
    plan=sub.add_parser('plan'); plan.add_argument('--fixture',required=True); plan.add_argument('--output')
    report=sub.add_parser('report'); report.add_argument('--kind')
    args=parser.parse_args(argv); database=Database(args.database)
    if args.command=='run':
        fixture=load_json(args.fixture); service=ApplicationService(database); outputs=[]
        for index,item in enumerate(fixture.get('items',[])):
            outputs.append(service.process(item,fixture.get('context',{}),item.get('request_key',f'item-{index}')))
        payload={'product':'人才驿站与服务窗口需求预测容量计划台','processed':len(outputs),'outputs':outputs,'report':Reporter(database).overview()}
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=_json_default),encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False,sort_keys=True,default=_json_default)); return 0
    if args.command=='plan':
        fixture=load_json(args.fixture); service=DemandPlanningService(database); results=[]
        items=fixture.get('items') or [fixture]
        base_options=_planning_payload(fixture)['options']
        for item in items:
            merged={**{key:value for key,value in fixture.items() if key!='items'},**item}
            request=_planning_payload(merged)
            request['options']={**base_options,**request['options']}
            results.append(_plan_one(service,request))
        payload={'product':'人才驿站与服务窗口需求预测容量计划台','processed':len(results),'results':results,'audit':service.audit()}
        text=json.dumps(payload,ensure_ascii=False,sort_keys=True,default=_json_default,indent=2)
        if args.output:
            Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(text+'\n',encoding='utf-8')
        print(text); return 0
    payload=Reporter(database).owner_breakdown(args.kind) if args.kind else Reporter(database).overview(); print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
