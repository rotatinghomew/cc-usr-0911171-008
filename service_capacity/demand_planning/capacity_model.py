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
 def backtest(self,observations,holdout=1,calendar=None,**options):
  as_of=options.pop('as_of',options.pop('decision_date',None))
  if as_of is not None:
   from .forecast_calibration import ForecastCalibrator
   horizon=int(options.pop('horizon',holdout))
   return ForecastCalibrator(capacity_model=self).rolling_backtest(observations,as_of,horizon=horizon,calendar=calendar,**options)
  train=observations[:-holdout] if holdout else observations;actual=observations[-holdout:] if holdout else [];pred=self.forecast(train,holdout or 1,calendar or {});errors=[]
  for expected,guess in zip(actual,pred):errors.append(abs(Decimal(str(expected['value']))-Decimal(guess['demand'])))
  return {'mae':str(sum(errors,Decimal('0'))/Decimal(len(errors))) if errors else None,'errors':[str(e) for e in errors]}
 def staffing(self,forecasts,service_minutes,shift_minutes,occupancy_target):
  target=Decimal(str(occupancy_target));rows=[]
  for row in forecasts:
   workload=Decimal(row['demand'])*Decimal(service_minutes);agents=(workload/(Decimal(shift_minutes)*target)).to_integral_value(rounding=ROUND_CEILING);rows.append({'date':row['date'],'agents':int(agents),'workload_minutes':str(workload)})
  return rows
 def queue_risk(self,demand,agents,service_per_agent):
  capacity=Decimal(agents)*Decimal(str(service_per_agent));util=Decimal(str(demand))/capacity if capacity else Decimal('Infinity');return {'utilization':str(util),'overloaded':util>Decimal('0.9'),'backlog':str(max(Decimal('0'),Decimal(str(demand))-capacity))}
 # -- point-in-time calibration and capacity decisions -----------------
 def visible_observations(self,observations,as_of,**options):
  from .forecast_calibration import visible_observations
  return visible_observations(observations,as_of,**options)
 def rolling_backtest(self,observations,as_of=None,horizon=1,calendar=None,**options):
  from .forecast_calibration import ForecastCalibrator
  return ForecastCalibrator(capacity_model=self).rolling_backtest(observations,as_of=as_of,horizon=horizon,calendar=calendar,**options)
 def calibrate(self,observations,as_of=None,horizon=1,calendar=None,**options):
  from .forecast_calibration import ForecastCalibrator
  return ForecastCalibrator(capacity_model=self).calibrate(observations,as_of=as_of,horizon=horizon,calendar=calendar,**options)
 def capacity_plan(self,calibration,capacity=None,calendar=None,**options):
  from .capacity_plan import CapacityPlanner
  return CapacityPlanner(**options).plan(calibration,capacity=capacity,calendar=calendar)
 def staffing_plan(self,*args,**options):
  if len(args)>=2 and isinstance(args[0],dict) and ('forecast_groups' in args[0] or 'forecast' in args[0]):
   return self.capacity_plan(args[0],args[1],options.pop('calendar',None),**options)
  forecasts=args[0] if args else options.get('forecast',[])
  service_minutes=args[1] if len(args)>1 else options.get('service_minutes',30)
  shift_minutes=args[2] if len(args)>2 else options.get('shift_minutes',480)
  occupancy=args[3] if len(args)>3 else options.get('occupancy_target','0.8')
  from .capacity_plan import CapacityPlanner,StaffingConstraints
  planner=CapacityPlanner(StaffingConstraints(service_minutes,shift_minutes,occupancy))
  return planner.plan({'forecast_groups':[{'site_id':'default','service_type':'default','deterministic':True,'forecast':forecasts}]},capacity=options.get('capacity'))
 def compare_plans(self,base,revised):
  from .capacity_plan import compare_plans
  return compare_plans(base,revised)
