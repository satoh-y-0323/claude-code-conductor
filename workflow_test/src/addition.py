def add(a: int | float, b: int | float) -> int | float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError(f"Unsupported operand types: {type(a).__name__} and {type(b).__name__}")
    return a + b
