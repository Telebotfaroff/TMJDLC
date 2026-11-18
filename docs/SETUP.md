# Colab setup

## 1. Open the notebook

[Open TMJDLC Google Drive Edition in Colab](https://colab.research.google.com/github/Telebotfaroff/TMJDLC/blob/drive/notebooks/TMJDLC_Drive.ipynb)

Run the notebook from top to bottom. It installs the `drive` branch, Chromium, mounts Google Drive and initializes the persistent library.

## 2. Configure the Telegram bot

Create a Telegram bot with BotFather. Set these environment variables in Colab (or in a local `.env` file):

- `TG_BOT_TOKEN`: bot token.
- `TG_ADMIN_IDS`: your numeric Telegram user ID(s), comma-separated.
- `TMJDLC_WORK_DIR`: temporary Colab download directory.
- `TMJDLC_MAX_PARALLEL_DOWNLOADS`: default download limit, 1–6.
- `TMJDLC_MAX_PARALLEL_UPLOADS`: simultaneous Drive uploads, 1–4.

The bot accepts only configured admin IDs. It uses Telegram Bot API for commands and progress; media files are uploaded directly to Google Drive.

## 3. Start the bot in Colab

After mounting Drive and setting the variables, run:

```bash
cd /content/TMJDLC
python -m tmjdlc.bot
```

Keep the Colab runtime alive while the bot is running.

## Telegram workflow

1. Send a Javtiful channel URL, or a single video URL.
2. For a channel, choose pages using `all`, `1,3,5-8`, etc.
3. Choose videos using the same syntax.
4. Select 1–6 parallel downloads.
5. The bounded pipeline downloads to temporary Colab storage, then uploads to Drive with the configured upload-worker limit.
6. Use the Telegram command buttons or `/status`, `/uploads`, and `/cancel` to control the bot.
7. Open `/uploads` and choose 1–4 simultaneous Google Drive uploads. The selected limit is saved in the Drive library database and restored after Colab restarts. Four upload worker tasks are available; the selected limit controls how many run at once.
8. `/cancel` cancels the current selection flow; already queued jobs continue.

Completed files are recorded in `database.json`, and the root `index.txt` is refreshed. Local job folders are deleted only after a verified upload and successful library/index update. If Drive upload fails, the local file is retained.

## Tests

```bash
python -m pytest -q
```

The browser resolver requires Chromium:

```bash
python -m playwright install --with-deps chromium
```

Do not run multiple Colab runtimes against the same JSON database. The JSON file is intended for one active worker runtime.
