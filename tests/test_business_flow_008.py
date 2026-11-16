import unittest
from service_capacity.demand_planning.capacity_model import CapacityModel
class CapacityModelTests(unittest.TestCase):
 def setUp(self):self.m=CapacityModel();self.rows=[{'date':'2026-11-01','value':10},{'date':'2026-11-02','value':12},{'date':'2026-11-03','value':14},{'date':'2026-11-04','value':16}]
 def test_forecast_trend(self):self.assertGreater(float(self.m.forecast(self.rows,1,{})[0]['demand']),16)
 def test_calendar_factor(self):self.assertGreater(float(self.m.forecast(self.rows,1,{'2026-11-05':{'factor':'2'}})[0]['demand']),30)
 def test_missing_day_forward_fill(self):self.assertEqual(len(self.m.normalize([self.rows[0],self.rows[2]])),3)
 def test_backtest(self):self.assertIn('mae',self.m.backtest(self.rows,1,{}))
 def test_staffing_rounds_up(self):self.assertEqual(self.m.staffing([{'date':'x','demand':'10'}],30,480,'0.8')[0]['agents'],1)
 def test_queue_risk(self):self.assertTrue(self.m.queue_risk(100,2,40)['overloaded'])
