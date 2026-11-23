"""Fake-data generators. Everything here is deliberately NON-REAL:

- IPs come from RFC 5737 documentation ranges (never routable, belong to nobody)
- Phone numbers use the 555-01XX block reserved for fiction
- Card numbers use a fake BIN and intentionally FAIL the Luhn check
- Bank accounts use a fake bank code and the word FAKE
Seeded by user ID so the same person always "leaks" the same joke data.
"""
import random

DOC_RANGES = ["192.0.2", "198.51.100", "203.0.113"]
CITIES = [
    "Narnia", "Gotham City", "Hogwarts", "Atlantis", "Bikini Bottom", "Springfield",
    "Mordor", "Wakanda", "Hill Valley", "Duckburg", "Candy Mountain", "Your Mom's House",
]
ISPS = [
    "Definitely Real ISP", "Potato Internet Co.", "Skibidi Broadband", "Hamster Wheel Networks",
    "Soggy Cable LLC", "Wi-Fi From Next Door Inc.", "Dial-Up Forever",
]
DEVICES = ["Toaster", "Smart Fridge", "Nokia 3310", "Windows XP Box", "Raspberry Pi in a Shoe", "Calculator"]


def rng_for(user_id: int, salt: str = "") -> random.Random:
    return random.Random(f"{user_id}:{salt}")


def fake_ip(r: random.Random) -> str:
    return f"{r.choice(DOC_RANGES)}.{r.randint(1, 254)}"


def fake_ipv6(r: random.Random) -> str:
    # 2001:db8::/32 is the documentation prefix
    return "2001:db8:" + ":".join(f"{r.randint(0, 0xFFFF):x}" for _ in range(3)) + "::1"


def fake_mac(r: random.Random) -> str:
    return ":".join(f"{r.randint(0, 255):02X}" for _ in range(6))


def fake_phone(r: random.Random) -> str:
    return f"(555) 555-01{r.randint(0, 99):02d}"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def fake_card(r: random.Random) -> str:
    while True:
        digits = "0000" + "".join(str(r.randint(0, 9)) for _ in range(12))
        if not _luhn_ok(digits):  # guarantee it can never validate
            break
    return " ".join(digits[i:i + 4] for i in range(0, 16, 4))


def fake_bank(r: random.Random) -> dict:
    return {
        "bank": r.choice(["Bank of Nowhere", "First Fake National", "Monopoly Money Bank", "Piggy Savings & Loan"]),
        "account": "FAKE-" + "".join(str(r.randint(0, 9)) for _ in range(8)),
        "routing": "000000000",
        "card": fake_card(r),
        "exp": f"{r.randint(1, 12):02d}/{r.randint(30, 39)}",
        "cvv": "000",
        "balance": f"${r.randint(0, 99):d},{r.randint(0, 999):03d}.{r.randint(0, 99):02d}",
  }
  
