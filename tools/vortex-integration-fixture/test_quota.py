import unittest
from quota import can_reserve, remaining_capacity


class QuotaContract(unittest.TestCase):
    def test_reservation_boundary(self):
        self.assertTrue(can_reserve(3, 6, 10))
        self.assertTrue(can_reserve(3, 7, 10))
        self.assertFalse(can_reserve(3, 8, 10))

    def test_zero_capacity(self):
        self.assertTrue(can_reserve(0, 0, 0))
        self.assertFalse(can_reserve(0, 1, 0))

    def test_negative_counts_rejected(self):
        for args in [(-1, 0, 1), (0, -1, 1), (0, 0, -1)]:
            with self.assertRaises(ValueError):
                can_reserve(*args)

    def test_remaining_capacity_control(self):
        self.assertEqual(remaining_capacity(3, 10), 7)
        self.assertEqual(remaining_capacity(10, 10), 0)
        self.assertEqual(remaining_capacity(11, 10), 0)
        for args in [(-1, 10), (0, -1)]:
            with self.assertRaises(ValueError):
                remaining_capacity(*args)


if __name__ == "__main__":
    unittest.main()
