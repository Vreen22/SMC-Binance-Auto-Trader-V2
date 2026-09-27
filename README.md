# SMC Binance Auto Trader V2
Separate project. Testnet first.

1. Copy `.env.example` to `.env`.
2. Add Binance Futures Testnet API credentials.
3. `pip install -r requirements.txt`
4. `uvicorn backend.server:app --host 0.0.0.0 --port 8000`
5. Open `index.html`.
6. Test backend.
7. Keep AUTO TRADE OFF until the setup is verified.
8. Turn AUTO TRADE ON only for Testnet testing.

The frontend only sends a signal after all five rule groups are confirmed.
The backend enforces one active trade, max trade size, duplicate protection,
emergency stop, STOP_MARKET SL and TAKE_PROFIT_MARKET TP2.

Never put API secrets in GitHub. This is a rule-based trading system and does not guarantee profit.
