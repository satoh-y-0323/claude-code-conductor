import pytest
from src.multiplication import multiply


class TestMultiply:
    def test_multiply_integers(self):
        assert multiply(2, 3) == 6

    def test_multiply_floats(self):
        assert multiply(1.5, 2.0) == 3.0

    def test_multiply_by_zero(self):
        assert multiply(5, 0) == 0

    def test_multiply_negative(self):
        assert multiply(-2, 3) == -6

    def test_multiply_non_numeric_raises_type_error(self):
        with pytest.raises(TypeError):
            multiply("a", 1)
