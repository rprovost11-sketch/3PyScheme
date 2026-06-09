"""Exact rational number type: replaces fractions.Fraction.

_Rat(num, den=1) -- num/den normalized by GCD, denominator always positive.
Supports: +, -, *, /, **, neg, abs, float, comparisons with int/_Rat/float,
and __floor__/__ceil__/__trunc__/__round__ so math.floor/ceil/trunc/round work.
"""

import math


class _Rat:
    __slots__ = ('numerator', 'denominator')

    def __init__(self, num, den=1):
        if isinstance(num, _Rat) and den == 1:
            self.numerator = num.numerator
            self.denominator = num.denominator
            return
        n = int(num)
        d = int(den)
        if d == 0:
            raise ZeroDivisionError('rational denominator is zero')
        if d < 0:
            n = -n
            d = -d
        g = math.gcd(abs(n), d)
        self.numerator = n // g
        self.denominator = d // g

    @staticmethod
    def from_float(f):
        n, d = f.as_integer_ratio()
        return _Rat(n, d)

    def __float__(self):
        return self.numerator / self.denominator

    def __int__(self):
        n, d = self.numerator, self.denominator
        if n < 0:
            return -((-n) // d)
        return n // d

    def __bool__(self):
        return self.numerator != 0

    def __neg__(self):
        r = object.__new__(_Rat)
        r.numerator = -self.numerator
        r.denominator = self.denominator
        return r

    def __abs__(self):
        if self.numerator >= 0:
            return self
        r = object.__new__(_Rat)
        r.numerator = -self.numerator
        r.denominator = self.denominator
        return r

    def __pos__(self):
        return self

    def __add__(self, other):
        if isinstance(other, int):
            return _Rat(self.numerator + other * self.denominator, self.denominator)
        if isinstance(other, _Rat):
            return _Rat(self.numerator * other.denominator + other.numerator * self.denominator,
                        self.denominator * other.denominator)
        if isinstance(other, float):
            return float(self) + other
        return NotImplemented

    def __radd__(self, other):
        if isinstance(other, int):
            return _Rat(other * self.denominator + self.numerator, self.denominator)
        if isinstance(other, float):
            return other + float(self)
        return NotImplemented

    def __sub__(self, other):
        if isinstance(other, int):
            return _Rat(self.numerator - other * self.denominator, self.denominator)
        if isinstance(other, _Rat):
            return _Rat(self.numerator * other.denominator - other.numerator * self.denominator,
                        self.denominator * other.denominator)
        if isinstance(other, float):
            return float(self) - other
        return NotImplemented

    def __rsub__(self, other):
        if isinstance(other, int):
            return _Rat(other * self.denominator - self.numerator, self.denominator)
        if isinstance(other, float):
            return other - float(self)
        return NotImplemented

    def __mul__(self, other):
        if isinstance(other, int):
            return _Rat(self.numerator * other, self.denominator)
        if isinstance(other, _Rat):
            return _Rat(self.numerator * other.numerator,
                        self.denominator * other.denominator)
        if isinstance(other, float):
            return float(self) * other
        return NotImplemented

    def __rmul__(self, other):
        if isinstance(other, int):
            return _Rat(other * self.numerator, self.denominator)
        if isinstance(other, float):
            return other * float(self)
        return NotImplemented

    def __truediv__(self, other):
        if isinstance(other, int):
            return _Rat(self.numerator, self.denominator * other)
        if isinstance(other, _Rat):
            return _Rat(self.numerator * other.denominator,
                        self.denominator * other.numerator)
        if isinstance(other, float):
            return float(self) / other
        return NotImplemented

    def __rtruediv__(self, other):
        if isinstance(other, int):
            return _Rat(other * self.denominator, self.numerator)
        if isinstance(other, float):
            return other / float(self)
        return NotImplemented

    def __pow__(self, exp):
        if not isinstance(exp, int):
            return NotImplemented
        if exp == 0:
            return _Rat(1)
        if exp > 0:
            return _Rat(self.numerator ** exp, self.denominator ** exp)
        p = self.denominator ** (-exp)
        q = self.numerator ** (-exp)
        return _Rat(p, q)

    def __eq__(self, other):
        if isinstance(other, int):
            return self.denominator == 1 and self.numerator == other
        if isinstance(other, _Rat):
            return self.numerator == other.numerator and self.denominator == other.denominator
        if isinstance(other, float):
            return float(self) == other
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, int):
            return self.numerator < other * self.denominator
        if isinstance(other, _Rat):
            return self.numerator * other.denominator < other.numerator * self.denominator
        if isinstance(other, float):
            return float(self) < other
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, int):
            return self.numerator <= other * self.denominator
        if isinstance(other, _Rat):
            return self.numerator * other.denominator <= other.numerator * self.denominator
        if isinstance(other, float):
            return float(self) <= other
        return NotImplemented

    def __gt__(self, other):
        if isinstance(other, int):
            return self.numerator > other * self.denominator
        if isinstance(other, _Rat):
            return self.numerator * other.denominator > other.numerator * self.denominator
        if isinstance(other, float):
            return float(self) > other
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, int):
            return self.numerator >= other * self.denominator
        if isinstance(other, _Rat):
            return self.numerator * other.denominator >= other.numerator * self.denominator
        if isinstance(other, float):
            return float(self) >= other
        return NotImplemented

    def __hash__(self):
        if self.denominator == 1:
            return hash(self.numerator)
        return hash((self.numerator, self.denominator))

    def __floor__(self):
        return self.numerator // self.denominator

    def __ceil__(self):
        n, d = self.numerator, self.denominator
        return -(-n // d)

    def __trunc__(self):
        n, d = self.numerator, self.denominator
        if n < 0:
            return -((-n) // d)
        return n // d

    def __round__(self, ndigits=None):
        n, d = self.numerator, self.denominator
        floor_n = n // d
        rem = n - floor_n * d
        twice = 2 * rem
        if twice < d:
            return floor_n
        if twice > d:
            return floor_n + 1
        return floor_n if floor_n % 2 == 0 else floor_n + 1

    def __repr__(self):
        return '_Rat(%d, %d)' % (self.numerator, self.denominator)
