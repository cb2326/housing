"""Transverse Mercator for Armenia's national grid (ARMREF02 / ARM-TM): GRS80, central meridian 45E,
scale 0.9999, false easting 8,500,000 m, false northing 0 (assumed; checked against known places)."""
import math
A, F = 6378137.0, 1 / 298.257222101
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
LON0, K0, FE, FN = math.radians(45), 0.9999, 8500000.0, 0.0


def _m(phi):
    e4, e6 = E2 * E2, E2 ** 3
    return A * ((1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi) - (35 * e6 / 3072) * math.sin(6 * phi))


def forward(lon, lat):
    phi, lam = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    t, c = math.tan(phi) ** 2, EP2 * math.cos(phi) ** 2
    a = (lam - LON0) * math.cos(phi)
    x = K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5 / 120)
    y = K0 * (_m(phi) + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24 + (61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6 / 720))
    return x + FE, y + FN


def inverse(x, y):
    x -= FE
    m = (y - FN) / K0
    mu = m / (A * (1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    phi1 = mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu) \
        + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu)
    n1 = A / math.sqrt(1 - E2 * math.sin(phi1) ** 2)
    t1, c1 = math.tan(phi1) ** 2, EP2 * math.cos(phi1) ** 2
    r1 = A * (1 - E2) / (1 - E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4 / 24
                                              + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = LON0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(lon), math.degrees(lat)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("Annex 2 point 1 ->", inverse(8458622.885, 4450978.107))
    print("Republic Square 44.5126,40.1777 ->", forward(44.5126, 40.1777), "-> back", inverse(*forward(44.5126, 40.1777)))
