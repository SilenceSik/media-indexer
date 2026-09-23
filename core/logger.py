import logging
import os


os.makedirs(
    "storage/logs",
    exist_ok=True
)


logging.basicConfig(
    filename="storage/logs/app.log",
    level=logging.INFO,
    format=
    "%(asctime)s %(levelname)s %(message)s"
)


def info(message):

    logging.info(
        message
    )


def error(message):

    logging.error(
        message
    )
