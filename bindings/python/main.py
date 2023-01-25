import argparse
from rgbmatrix import RGBMatrix, RGBMatrixOptions
import schedule
from time import sleep
from datetime import datetime, timedelta
import logging
import sys
import os
import requests

from weather import Weather
from stocks import Stocks, Market
from imageviewer import ImageViewer

from fastapi import FastAPI
import asyncio

app = FastAPI()

log = logging.getLogger()
path = os.path.dirname(__file__) + '/'
logging.basicConfig(level=logging.INFO, \
                            filename=path + 'log.txt', \
                            format='[%(asctime)s] %(levelname)-8s (%(name)s) %(message)s', datefmt='%H:%M:%S') # stream=sys.stdout)

# Configuration for the matrix
options = RGBMatrixOptions()
options.rows = 32
options.cols = 64
options.chain_length = 1
options.parallel = 1
options.hardware_mapping = 'adafruit-hat-pwm'  # If you have an Adafruit HAT: 'adafruit-hat'

matrix = RGBMatrix(options = options)
apps = list()
view = list()

def log_schedule():
    log.info("Scheduled jobs: %s" % schedule.get_jobs())

@app.on_event("startup")
async def startup_event():
    global apps, view
    #apps.append(Weather(matrix, 37.384, 122.027))
    apps.append(ImageViewer(matrix, path + "images/nvidia.png"))
    apps.append(Stocks(matrix, "NVDA"))
    apps.append(Stocks(matrix, "VTI"))
    for app in apps:
        view.append(app)

    schedule.every(5).minutes.do(log_schedule).tag('system')

    asyncio.create_task(runner())

@app.get("/")
async def root():
    return {"message": "Hello World"}

@app.get("/off")
async def off():
    global view
    view.clear()

@app.get("/logo")
async def on():
    global view, apps
    view.append(apps[0])

@app.get("/nvda")
async def nvda():
    global view, apps
    view.append(apps[1])

async def runner():

    log_schedule()
    duration = 6
    while True:
        schedule.run_pending()
        for app in view:
            framerate = app.get_framerate()
            for sec in range(0,framerate*duration):
                matrix.SwapOnVSync(app.show())
                await asyncio.sleep(1/framerate)
        if len(view) == 0:
            matrix.Clear()
            await asyncio.sleep(1)

