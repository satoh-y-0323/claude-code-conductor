def multiply(a: int | float, b: int | float) -> int | float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError(f"引数は数値型である必要があります: a={type(a)}, b={type(b)}")
    return a * b
