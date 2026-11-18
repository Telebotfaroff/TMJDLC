# Google Colab setup plan

The final notebook will install project dependencies, mount Google Drive, authenticate the current Colab user for Drive API access, load Telegram Bot API credentials, create the temporary work directory, initialize the Drive root folder plus `database.json` and `index.txt`, and run a startup self-check before starting polling and workers.

Telegram API ID/hash are not required because Telegram is used through the Bot API only. Downloaded videos are temporary in Colab and permanent in Google Drive.

Do not run an incomplete notebook. The notebook will be added after the modules it launches exist and pass tests.
