def multiply(a: int | float, b: int | float) -> int | float:
    """2 つの数値を乗算して返す。

    Args:
        a: 1 つ目のオペランド（int または float）
        b: 2 つ目のオペランド（int または float）

    Returns:
        a * b の結果

    Raises:
        TypeError: a または b が数値型でない場合（bool は除外）
    """
    if isinstance(a, bool) or isinstance(b, bool) or \
       not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("Both arguments must be numeric (int or float)")
    return a * b
