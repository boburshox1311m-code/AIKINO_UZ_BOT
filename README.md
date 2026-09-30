# AIKINO_UZ Telegram bot

Professional Uzbek-language movie/series catalogue bot with a phone-friendly admin panel.

## Project ownership

- **Company:** AIKINOUZ
- **President:** BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON UGLI
- **Email:** boburshox1311m@gmail.com
- **Project:** AIKINOUZ / AIKINO_UZ_BOT

### Copyright

© 2026 AIKINOUZ.  
Project owner / author attribution: **BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON UGLI**.  
All rights reserved.

## Environment variables

- `BOT_TOKEN` — token from BotFather (secret)
- `ADMIN_ID` — numeric Telegram user ID of the owner
- `DATABASE_URL` — PostgreSQL connection URL (Railway injects this)

## Railway

1. Create a service from this GitHub repository.
2. Add a PostgreSQL database to the same Railway project.
3. Add `BOT_TOKEN` and `ADMIN_ID` to the bot service variables.
4. Reference the PostgreSQL `DATABASE_URL` in the bot service.

Start command: `python -m bot`

## Local check

```bash
python -m compileall bot
```
