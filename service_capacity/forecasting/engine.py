from __future__ import annotations
from ..demand_planning.capacity_model import CapacityModel
import datetime as dt
import decimal
import math
import statistics
import ast
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

class DomainError(RuntimeError):
    pass

@dataclass(frozen=True)
class AnalysisResult:
    key: str
    accepted: bool
    value: decimal.Decimal
    reasons: tuple[str,...]
    facts: Mapping[str,Any]=field(default_factory=dict)

class ForecastEngineBase:
    algorithm='forecast'
    def normalize(self,rows):
        result=[];seen=set()
        for position,row in enumerate(rows):
            item=dict(row);key=str(item.get('id') or position)
            if key in seen:continue
            seen.add(key);item['_key']=key;item['_position']=position;result.append(item)
        return result
    def rank(self,subjects,context):return sorted([self.evaluate(item,context) for item in self.normalize(subjects)],key=lambda result:(not result.accepted,-result.value,result.key))
    def allocate(self,subjects,context):
        remaining=decimal.Decimal(str(context.get('capacity',100)))-decimal.Decimal(str(context.get('reserved',0)));accepted=[];rejected=[]
        by_key={str(item.get('id')):item for item in self.normalize(subjects)}
        for result in self.rank(subjects,context):
            demand=decimal.Decimal(str(by_key[result.key].get('demand',1)))
            if result.accepted and demand<=remaining:accepted.append(result);remaining-=demand
            else:rejected.append(AnalysisResult(result.key,False,result.value,result.reasons+('allocation_exhausted',),result.facts))
        return accepted,rejected
    def summarize(self,results):
        values=[float(result.value) for result in results];reasons=Counter(reason for result in results for reason in result.reasons)
        return {'count':len(results),'accepted':sum(result.accepted for result in results),'rejected':sum(not result.accepted for result in results),'mean':statistics.fmean(values) if values else 0.0,'median':statistics.median(values) if values else 0.0,'reasons':dict(sorted(reasons.items()))}
    def compare(self,left,right):
        a={item.key:item for item in left};b={item.key:item for item in right};changes=[]
        for key in sorted(set(a)|set(b)):
            old=a.get(key);new=b.get(key)
            if old is None:changes.append({'key':key,'change':'added','accepted':new.accepted})
            elif new is None:changes.append({'key':key,'change':'removed','accepted':old.accepted})
            elif old.accepted!=new.accepted or old.value!=new.value:changes.append({'key':key,'change':'changed','from':str(old.value),'to':str(new.value),'accepted_from':old.accepted,'accepted_to':new.accepted})
        return {'changes':changes,'change_count':len(changes)}

class ForecastEngine(ForecastEngineBase):
    def __init__(self): self.capacity_model=CapacityModel()
    def evaluate(self,item,context):
        if item.get('observations'):
            forecast=self.capacity_model.forecast(item['observations'],int(context.get('horizon',1)),context.get('calendar',{})); value=decimal.Decimal(forecast[0]['demand']); capacity=decimal.Decimal(str(context.get('capacity','999999'))); return AnalysisResult(str(item.get('id','unknown')),value<=capacity,value,() if value<=capacity else ('forecast_exceeds_capacity',),{'forecast':forecast})
        key=str(item.get('id','unknown'));history=[decimal.Decimal(str(value)) for value in item.get('history',item.get('values',[])) if value is not None];reasons=[]
        if len(history)<3:reasons.append('insufficient_history')
        window=max(1,min(int(context.get('window',3)),len(history)));baseline=sum(history[-window:],decimal.Decimal('0'))/decimal.Decimal(window) if history else decimal.Decimal('0');trend=(history[-1]-history[0])/decimal.Decimal(max(1,len(history)-1)) if len(history)>1 else decimal.Decimal('0');season=decimal.Decimal(str(context.get('season_index',1)));forecast=max(decimal.Decimal('0'),(baseline+trend)*season)
        capacity=decimal.Decimal(str(context.get('capacity',10**9)));accepted=not reasons and forecast<=capacity
        if forecast>capacity:reasons.append('forecast_exceeds_capacity')
        return AnalysisResult(key,accepted,forecast,tuple(reasons),{'baseline':str(baseline),'trend':str(trend),'season_index':str(season),'history_points':len(history)})
