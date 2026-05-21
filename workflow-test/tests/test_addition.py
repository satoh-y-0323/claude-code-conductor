import pytest
from addition import add


class TestAddNormalCases:
    def test_add_two_integers(self):
        assert add(2, 3) == 5

    def test_add_two_floats(self):
        assert add(1.5, 2.5) == 4.0

    def test_add_int_and_float(self):
        assert add(1, 0.5) == 1.5

    def test_add_negative_numbers(self):
        assert add(-3, -2) == -5

    def test_add_with_zero(self):
        assert add(0, 5) == 5


class TestAddErrorCases:
    def test_add_string_raises_type_error(self):
        with pytest.raises(TypeError):
            add("1", 2)

    def test_add_none_raises_type_error(self):
        with pytest.raises(TypeError):
            add(None, 2)

    def test_add_list_raises_type_error(self):
        with pytest.raises(TypeError):
            add([1], 2)

    def test_add_bool_raises_type_error(self):
        with pytest.raises(TypeError):
            add(True, 2)
