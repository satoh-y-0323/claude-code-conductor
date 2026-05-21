import pytest
from src.addition import add


class TestAddNormalCases:
    def test_add_two_integers(self):
        assert add(1, 2) == 3

    def test_add_floats(self):
        assert add(1.5, 2.5) == 4.0

    def test_add_negative_numbers(self):
        assert add(-1, -2) == -3

    def test_add_zeros(self):
        assert add(0, 0) == 0


class TestAddErrorCases:
    def test_add_string_raises_type_error(self):
        with pytest.raises(TypeError):
            add("a", 1)
