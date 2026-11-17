from __future__ import annotations
import datetime as dt
import uuid
from typing import Any, Mapping, Sequence
from .contracts import Event, fingerprint
from .storage import Database
from .forecasting.engine import ForecastEngine
from .demand_planning.forecast_calibration import ForecastCalibrator
from .demand_planning.capacity_plan import CapacityPlanner

_PLANNING_KEYS=('as_of','decision_date','cutoff')

def _planning_request(payload:Mapping[str,Any],context:Mapping[str,Any]):
    """Return a planning request when the payload carries planning context.

    The legacy forecast path is always preserved; planning fields are additive.
    A request needs observations plus a decision date.  When no decision date
    is supplied the latest observation day is used, which keeps the result
    deterministic rather than silently depending on wall-clock time.
    """

    observations=payload.get('observations') or payload.get('history_rows')
    if not observations:return None
    as_of=None
    for source in (payload,context):
        for key in _PLANNING_KEYS:
            if source.get(key) is not None:
                as_of=source[key];break
        if as_of is not None:break
    if as_of is None:
        dates=[]
        for row in observations if isinstance(observations,(list,tuple)) else []:
            if isinstance(row,Mapping) and row.get('date') is not None:
                dates.append(str(row['date']))
        as_of=max(dates) if dates else None
    if as_of is None:return None
    calendar=payload.get('calendar',context.get('calendar',{}))
    capacity=payload.get('capacity',payload.get('roster',context.get('capacity_agents',context.get('roster'))))
    horizon=int(payload.get('horizon',context.get('horizon',1)))
    constraints={}
    for key in ('service_minutes','shift_minutes','occupancy_target','overload_threshold','maximum_agents'):
        if key in payload:constraints[key]=payload[key]
        elif key in context:constraints[key]=context[key]
    planner_options={key:constraints[key] for key in ('service_minutes','shift_minutes','occupancy_target','overload_threshold','maximum_agents') if key in constraints}
    options={}
    for key in ('confidence','min_factor','max_factor','minimum_history','min_samples','train_window','strict_point_in_time','site_id','service_type'):
        if key in payload:options[key]=payload[key]
        elif key in context:options[key]=context[key]
    adjustments=payload.get('adjustments',context.get('adjustments'))
    return {'observations':observations,'as_of':as_of,'calendar':calendar,'capacity':capacity,'horizon':horizon,'constraints':constraints,'planner_options':planner_options,'options':options,'adjustments':adjustments}

class ApplicationService:
    def __init__(self,database:Database):
        self.database=database; self.engine=ForecastEngine(); self.calibrator=ForecastCalibrator(); self.planner=CapacityPlanner()

    def process(self,payload:Mapping[str,Any],context:Mapping[str,Any],request_key:str) -> dict[str,Any]:
        digest=fingerprint({'payload':payload,'context':context})
        recalled=self.database.recalled('process',request_key,digest)
        if recalled is not None:return recalled
        result=self.engine.evaluate(payload,context)
        identifier=result.key
        response={'id':identifier,'accepted':result.accepted,'value':str(result.value),'reasons':list(result.reasons),'facts':dict(result.facts)}
        planning=_planning_request(payload,context)
        if planning is not None:
            calibration=self.calibrator.calibrate(planning['observations'],planning['as_of'],horizon=planning['horizon'],calendar=planning['calendar'],**planning['options'])
            planner=CapacityPlanner(**planning['planner_options']) if planning['planner_options'] else self.planner
            plan=planner.plan(calibration,capacity=planning['capacity'],calendar=planning['calendar'])
            response['calibration']=calibration
            response['capacity_plan']=plan
            response['plan']=plan
            response['staffing']=plan.get('recommendations',plan.get('rows',[]))
            response['risk']=plan.get('status')
            response['planning']={'as_of':calibration['as_of'],'source_fingerprint':calibration['source_fingerprint'],'deterministic':calibration['deterministic'],'status':calibration['status']}
            if planning['adjustments'] is not None:
                revised=self.planner.revise(plan,planning['adjustments'])
                revised['comparison']=self.planner.compare(plan,revised)
                response['revision']=revised
        with self.database.transaction() as connection:
            record={'site_id':identifier,'owner_id':str(payload.get('owner_id','owner-default')),'category':'forecast','status':'active' if result.accepted else 'cancelled','effective_from':str(payload.get('effective_from',dt.date.today())),'effective_to':payload.get('effective_to'),'quantity':int(payload.get('demand',0)),'amount':str(payload.get('amount','0')),'attributes':response}
            version=self.database.put_record(connection,'site',record)
            event=Event(str(uuid.uuid4()),'site',identifier,'processed',dt.datetime.now(dt.timezone.utc),response,version)
            self.database.append_event(connection,event)
            self.database.enqueue(connection,'result:'+identifier,'forecast'+'.result',identifier,response)
            self.database.remember(connection,'process',request_key,digest,response)
        return response

    def batch(self,subjects:Sequence[Mapping[str,Any]],context:Mapping[str,Any]) -> dict[str,Any]:
        results=self.engine.rank(subjects,context)
        return {'summary':self.engine.summarize(results),'results':[{'id':r.key,'accepted':r.accepted,'value':str(r.value),'reasons':list(r.reasons)} for r in results]}

    def calibrate(self,observations:Any,as_of:Any,**options:Any) -> dict[str,Any]:
        return self.calibrator.calibrate(observations,as_of,**options)

    def backtest(self,observations:Any,as_of:Any,**options:Any) -> dict[str,Any]:
        return self.calibrator.rolling_backtest(observations,as_of,**options)

    def plan(self,calibration:Mapping[str,Any],capacity:Any=None,calendar:Any=None) -> dict[str,Any]:
        return self.planner.plan(calibration,capacity=capacity,calendar=calendar)

    def revise(self,plan:Mapping[str,Any],adjustments:Any) -> dict[str,Any]:
        revised=self.planner.revise(plan,adjustments); revised['comparison']=self.planner.compare(plan,revised); return revised

    def compare(self,base:Mapping[str,Any],revised:Mapping[str,Any]) -> dict[str,Any]:
        return self.planner.compare(base,revised)
