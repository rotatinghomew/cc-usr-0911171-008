import decimal
import unittest
from service_capacity.forecasting.engine import ForecastEngine

class DomainTests(unittest.TestCase):
    def setUp(self): self.engine=ForecastEngine()

    def series(self,**changes):
        value={'id':'site-1','history':[10,12,14,16]};value.update(changes);return value
    def context(self,**changes):
        value={'window':3,'season_index':'1','capacity':'30'};value.update(changes);return value
    def test_forecast(self):self.assertEqual(self.engine.evaluate(self.series(),self.context()).value,decimal.Decimal('16'))
    def test_insufficient_history(self):self.assertIn('insufficient_history',self.engine.evaluate(self.series(history=[1,2]),self.context()).reasons)
    def test_capacity_breach(self):self.assertIn('forecast_exceeds_capacity',self.engine.evaluate(self.series(),self.context(capacity='10')).reasons)
    def test_seasonality(self):self.assertEqual(self.engine.evaluate(self.series(),self.context(season_index='2')).value,decimal.Decimal('32'))
    def test_nonnegative(self):self.assertGreaterEqual(self.engine.evaluate(self.series(history=[5,3,1]),self.context()).value,0)
    def test_rank_high_forecast(self):self.assertEqual(self.engine.rank([self.series(id='low',history=[1,1,1]),self.series(id='high')],self.context())[0].key,'high')

