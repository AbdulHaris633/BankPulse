# Adspower Selenium Bot

This repo contains an AdsPower API client which is a wrapper for AdsPower and implements all CRUD operations like create browser, delete browser, open/close, and change proxy. 

There is a Browser class which uses the AdsPower API. The Browser class is meant to be extended to create bots. Like SocialMediaBot(Browser)

Browser all contains a client for the TwoCaptcha API as an example. 

Secrets (like AdsPower API key) are stored in the .env file (see .env.example)