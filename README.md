# Favorite Hunter

Paper-trading scanner for Polymarket favorites. It asks one question:

> Which high-probability Polymarket outcome is priced **lower** than its realistic probability by enough to justify the risk?

**Paper trading only.** No code path places, signs or cancels real orders.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
favorite-hunter verify          # live checks against the Polymarket APIs
favorite-hunter scan            # favorites with executable price 0.80-0.98
pytest                          # unit tests
```

The scanner needs outbound HTTPS to `gamma-api.polymarket.com`, `clob.polymarket.com`
and `data-api.polymarket.com`. When a source is unreachable it prints
`DATA UNAVAILABLE` with the reason and never substitutes values.
