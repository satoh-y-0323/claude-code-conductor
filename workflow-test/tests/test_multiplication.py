import pytest
from multiplication import multiply


class TestMultiplyNormal:
    def test_multiply_two_integers(self):
        assert multiply(3, 4) == 12

    def test_multiply_two_floats(self):
        assert multiply(1.5, 2.0) == 3.0

    def test_multiply_int_and_float(self):
        assert multiply(2, 0.5) == 1.0

    def test_multiply_negative_numbers(self):
        assert multiply(-3, -2) == 6

    def test_multiply_with_zero(self):
        assert multiply(0, 5) == 0


class TestMultiplyTypeError:
    def test_multiply_string_raises_type_error(self):
        with pytest.raises(TypeError):
            multiply("2", 3)

    def test_multiply_none_raises_type_error(self):
        with pytest.raises(TypeError):
            multiply(None, 3)

    def test_multiply_list_raises_type_error(self):
        with pytest.raises(TypeError):
            multiply([1], 3)

    def test_multiply_bool_raises_type_error(self):
        with pytest.raises(TypeError):
            multiply(True, 3)
