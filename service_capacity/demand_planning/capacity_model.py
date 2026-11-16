from __future__ import annotations
import datetime as dt
import statistics
from decimal import Decimal,ROUND_CEILING
class CapacityModel:
 def normalize(self,observations):
  by_day={dt.date.fromisoformat(str(row['date'])):row.get('value') for row in observations};start=min(by_day);end=max(by_day);result=[];cursor=start;last=None
  while cursor<=end:
   value=by_day.get(cursor)
   if value is None:value=last
   if value is not None:last=Decimal(str(value));result.append((cursor,last))
   cursor+=dt.timedelta(days=1)
  return result
 def forecast(self,observations,horizon,calendar):
  series=self.normalize(observations);values=[value for _,value in series];window=min(7,len(values));baseline=values[-1];trend=(values[-1]-values[max(0,len(values)-window)])/Decimal(max(1,window-1));forecasts=[]
  last_day=series[-1][0]
  for offset in range(1,horizon+1):
   day=last_day+dt.timedelta(days=offset);season=Decimal(str(calendar.get(day.isoformat(),{}).get('factor','1')));forecasts.append({'date':day.isoformat(),'demand':str(max(Decimal('0'),(baseline+trend*offset)*season))})
  return forecasts
 def backtest(self,observations,holdout,calendar):
  train=observations[:-holdout];actual=observations[-holdout:];pred=self.forecast(train,holdout,calendar);errors=[]
  for expected,guess in zip(actual,pred):errors.append(abs(Decimal(str(expected['value']))-Decimal(guess['demand'])))
  return {'mae':str(sum(errors,Decimal('0'))/Decimal(len(errors))),'errors':[str(e) for e in errors]}
 def staffing(self,forecasts,service_minutes,shift_minutes,occupancy_target):
  target=Decimal(str(occupancy_target));rows=[]
  for row in forecasts:
   workload=Decimal(row['demand'])*Decimal(service_minutes);agents=(workload/(Decimal(shift_minutes)*target)).to_integral_value(rounding=ROUND_CEILING);rows.append({'date':row['date'],'agents':int(agents),'workload_minutes':str(workload)})
  return rows
 def queue_risk(self,demand,agents,service_per_agent):
  capacity=Decimal(agents)*Decimal(str(service_per_agent));util=Decimal(str(demand))/capacity if capacity else Decimal('Infinity');return {'utilization':str(util),'overloaded':util>Decimal('0.9'),'backlog':str(max(Decimal('0'),Decimal(str(demand))-capacity))}
