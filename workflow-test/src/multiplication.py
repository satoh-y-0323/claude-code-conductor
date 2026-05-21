def multiply(a: int | float, b: int | float) -> int | float:
    """二つの数値を掛け算して返す。

    Args:
        a: 乗算する数値（int または float）
        b: 乗算する数値（int または float）

    Returns:
        a と b の積

    Raises:
        TypeError: a または b が int/float でない場合（bool を含む）
    """
    # bool は int のサブクラスのため、isinstance(True, int) は True を返す
    # そのため bool を明示的に除外する
    if isinstance(a, bool) or not isinstance(a, (int, float)):
        raise TypeError(f"引数 a は int または float でなければなりません。{type(a)} が渡されました。")
    if isinstance(b, bool) or not isinstance(b, (int, float)):
        raise TypeError(f"引数 b は int または float でなければなりません。{type(b)} が渡されました。")
    return a * b
