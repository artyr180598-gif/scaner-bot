import math
import unittest
from dataclasses import fields

from cryptoforge_pro.ultimate_bot import Candle, TA, price


class UltimateTests(unittest.TestCase):
    def test_ema_constant(self):
        self.assertEqual(TA.ema([10.0] * 50, 20), 10.0)

    def test_rsi_uptrend(self):
        values = [float(i) for i in range(1, 40)]
        self.assertGreater(TA.rsi(values), 99)

    def test_atr_positive(self):
        candles = [Candle(i, 100 + i, 102 + i, 99 + i, 101 + i, 1000) for i in range(30)]
        self.assertGreater(TA.atr(candles), 0)

    def test_metrics_are_finite(self):
        candles = [Candle(i, 100 + i * .1, 101 + i * .1, 99 + i * .1, 100 + i * .1, 1000 + i) for i in range(250)]
        m = TA.metrics(candles)
        for f in fields(m):
            self.assertTrue(math.isfinite(float(getattr(m, f.name))))

    def test_price_format(self):
        self.assertEqual(price(100.0), "100.00")
        self.assertEqual(price(1.2345), "1.2345")
        self.assertEqual(price(.12345678), "0.123457")


if __name__ == "__main__":
    unittest.main()
