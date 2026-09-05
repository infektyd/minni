"""Small exhaustive control for the disposable inclusive capacity contract."""
import unittest
from quota import can_reserve, remaining_capacity


class QuotaGrid(unittest.TestCase):
    def test_small_nonnegative_domain(self):
        for capacity in range(10):
            for used in range(12):
                self.assertEqual(remaining_capacity(used, capacity), max(0, capacity - used))
                for requested in range(12):
                    self.assertEqual(can_reserve(used, requested, capacity), used + requested <= capacity)
