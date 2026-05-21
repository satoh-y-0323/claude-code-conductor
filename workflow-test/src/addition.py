def add(a: int | float, b: int | float) -> int | float:
    """2つの数値を加算して返す。

    a, b が int または float 以外（bool を含む）の場合は TypeError を送出する。
    bool は int のサブクラスだが、数値演算には不適切な型として除外する。
    """
    if isinstance(a, bool) or not isinstance(a, (int, float)):
        raise TypeError(f"a must be int or float, got {type(a).__name__}")
    if isinstance(b, bool) or not isinstance(b, (int, float)):
        raise TypeError(f"b must be int or float, got {type(b).__name__}")
    return a + b
