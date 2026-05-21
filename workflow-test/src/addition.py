def add(a: int | float, b: int | float) -> int | float:
    """Add two numeric values.

    Args:
        a: First numeric value (int or float).
        b: Second numeric value (int or float).

    Returns:
        Sum of a and b.

    Raises:
        TypeError: If a or b is not int or float (bool excluded).
    """
    if isinstance(a, bool) or not isinstance(a, (int, float)):
        raise TypeError(f"Both arguments must be int or float, not {type(a).__name__}")
    if isinstance(b, bool) or not isinstance(b, (int, float)):
        raise TypeError(f"Both arguments must be int or float, not {type(b).__name__}")
    return a + b
