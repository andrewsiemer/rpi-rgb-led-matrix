#!/usr/bin/env python
import json
import time
import requests
import zoneinfo
from datetime import datetime, timedelta
import pytz
import numpy
from twelvedata import TDClient, exceptions
from apscheduler.schedulers.background import BackgroundScheduler
from multiprocessing import Lock

from rgbmatrix import graphics
from secrets import STOCKS_API_KEY, LOCAL_TZ

import logging
log = logging.getLogger(__name__)

import os
path = os.path.dirname(__file__) + '/'

schedule = BackgroundScheduler(daemon=True)
schedule.start()

lock = Lock()

def _try_api(func):
    tries = 5
    timeout = 15
    while tries > 0:
        try:
            return func.as_json()
        except exceptions.BadRequestError:
            log.warning("API bad request using %s" % func.as_url())
            return False
        except exceptions.TwelveDataError:
            log.warning("API out of credits using %s trying again in %d seconds" % (func.as_url(), timeout))
            tries -= 1
            time.sleep(timeout)
    
    log.error("API errors continue after several attempts")
    exit()

def _try_request(url):
    tries = 5
    timeout = 15
    while tries > 0:
        try:
            return requests.get(url).json()
        except exceptions.TwelveDataError:
            log.warning("URL bad request using %s trying again in %d seconds" % (url, timeout))
            tries -= 1
            time.sleep(timeout)
    
    log.error("URL errors continue after several attempts")
    exit()

class Market:
    def __init__(self):
        self.exchange = "NYSE"
        self.timezone = "America/New_York"
        self.open_hour = 9
        self.open_min = 30
        self.open_time = 390 # minutes in stock day

        self.api_key = STOCKS_API_KEY
        self.td = TDClient(apikey=self.api_key)
        
        self.update_interval = 1 # update every 1 mins when open
        self.symbols = list()

        self._jobs = list()
        self._last_close_price = dict()
        self._trading_day_data = dict()

    def _at_open(self, dt):
        return dt.replace(hour=self.open_hour, minute=self.open_min, second=0, microsecond=0)

    def _is_trading_day(self, day):
        ts = self.td.time_series(
            symbol="NVDA",
            interval="1day",
            outputsize=1,
            start_date=day,
            end_date=day+timedelta(minutes=self.open_time),
            timezone=self.timezone
        )
        res = _try_api(ts)
        log.info("API _is_trading_day: %s -> %s" % (ts.as_url(), res))
        return res
    
    def _check_market_state(self):
        # update trading day
        trade_day = datetime.now(pytz.timezone(self.timezone))
        # is it before trading hours
        if (trade_day.hour*60+trade_day.minute) < (self.open_hour*60+self.open_min):
            trade_day = self._at_open(trade_day - timedelta(days=1))
        else:
            trade_day = self._at_open(trade_day)
        while trade_day.weekday() > 4 or not self._is_trading_day(trade_day):
            trade_day -= timedelta(days=1)
        prev_day = trade_day - timedelta(days=1)
        while prev_day.weekday() > 4 or not self._is_trading_day(prev_day):
            prev_day -= timedelta(days=1)

        lock.acquire()
        self.trading_day = self._at_open(trade_day)
        self.previous_day = self._at_open(prev_day)
    
        log.info("current trading day: %s" % self.trading_day.strftime('%Y-%m-%d'))
        log.info("previous trading day: %s" % self.previous_day.strftime('%Y-%m-%d'))
        lock.release()

        # update market status
        url = f"https://api.twelvedata.com/market_state?exchange={self.exchange}&apikey={self.api_key}"
        response = _try_request(url)
        log.info("API _check_market_state: %s -> %s" % (url, response))

        if response[0]['is_market_open']:
            time_to_close = response[0]['time_to_close'].split(":")
            self.next_update = datetime.now().replace(tzinfo=zoneinfo.ZoneInfo(LOCAL_TZ)) + timedelta(minutes=int(time_to_close[0])*60+int(time_to_close[1])+1)
            schedule.add_job(self._update_trading_day_data)
            if not schedule.get_jobs('_update_trading_day_data'):
                schedule.add_job(self._update_trading_day_data, 'interval', minutes=self.update_interval, id='_update_trading_day_data')
        else:
            time_to_open = response[0]['time_to_open'].split(":")
            self.next_update = datetime.now().replace(tzinfo=zoneinfo.ZoneInfo(LOCAL_TZ)) + timedelta(minutes=int(time_to_open[0])*60+int(time_to_open[1])+1)
            if schedule.get_jobs('_update_trading_day_data'):
                schedule.remove_job('_update_trading_day_data')
            self._update_trading_day_data()
        
        self._update_last_close_price()

        log.info("next trading day update: %s" % (self.next_update).strftime('%Y-%m-%d %H:%M'))
        schedule.add_job(self._check_market_state, 'date', run_date=self.next_update, id='_check_market_state')

    def _update_last_close_price(self):
        assert self.previous_day
        assert len(self.symbols) > 0
        ts = self.td.time_series(
            symbol=self.symbols,
            interval="1day",
            outputsize=1,
            start_date=self.previous_day,
            end_date=self.previous_day+timedelta(minutes=self.open_time),
            timezone=self.timezone
        )
        res = _try_api(ts)
        log.info("API _update_last_close_price: %s -> %s" % (ts.as_url(), res))
        lock.acquire()
        if self.symbols[0] in res: # many symbols
            self._last_close_price.clear()
            for symbol in self.symbols:
                self._last_close_price[symbol] = float(res[symbol][0]['close'])
        else: # only one symbol
            self._last_close_price.clear()
            self._last_close_price[self.symbols[0]] = float(res[0]['close'])
        lock.release()
    
    def _update_trading_day_data(self):
        assert self.trading_day
        assert len(self.symbols) > 0
        ts = self.td.time_series(
            symbol= self.symbols,
            interval="1min",
            start_date=self.trading_day,
            outputsize=self.open_time,
            end_date=self.trading_day+timedelta(minutes=self.open_time),
            timezone=self.timezone
        )
        res = _try_api(ts)
        log.info("API _update_trading_day_data: %s -> %s" % (ts.as_url(), res))
        lock.acquire()
        if self.symbols[0] in res: # many symbols
            self._trading_day_data.clear()
            self._trading_day_data = res
        else: # only one symbol
            self._trading_day_data.clear()
            self._trading_day_data[self.symbols[0]] = res
        lock.release()

    ############## Public Functions

    def add_symbol(self, symbol):
        self.symbols.append(symbol)
        if len(self.symbols) == 1:
            schedule.add_job(self._check_market_state)

    def remove_symbol(self, symbol):
        self.symbols.remove(symbol)

    def has_data(self, symbol):
        return symbol in self._trading_day_data and symbol in self._last_close_price

    def get_last_close_price(self, symbol):
        assert len(self._last_close_price) > 0
        return self._last_close_price[symbol]

    def get_trading_day_data(self, symbol):
        assert len(self._trading_day_data) > 0
        return self._trading_day_data[symbol]

    def get_current_price(self, symbol):
        assert len(self._trading_day_data) > 0
        return round(float(self._trading_day_data[symbol][0]['close']),2)

    def get_current_difference(self, symbol):
        assert len(self._trading_day_data) > 0
        return round(float(self._trading_day_data[symbol][0]['close'])-self.get_last_close_price(symbol),2)
    
    def get_current_percent(self, symbol):
        return round(self.get_current_difference(symbol)/self.get_last_close_price(symbol)*100,2)

market = Market()
class Stocks:
    def __init__(self, matrix, symbol):
        self.framerate = 1

        self.graph = self.Graph()
        self.matrix = matrix
        self.symbol = symbol
        
        market.add_symbol(self.symbol)
    
    def get_framerate(self):
        return self.framerate
    
    def __del__(self):
        market.remove_symbol(self.symbol)

    def show(self):
        return self.draw()

    def draw(self):
        lock.acquire()
        
        offscreen_canvas = self.matrix.CreateFrameCanvas()
        _tmp_canvas = self.matrix.CreateFrameCanvas()
        font = graphics.Font()
        font.LoadFont(path + "../../fonts/5x6.bdf")
        white = graphics.Color(255, 255, 255)
        grey = graphics.Color(155, 155, 155)
        red = graphics.Color(255, 0, 0)
        green = graphics.Color(0, 255, 0)

        graphics.DrawText(offscreen_canvas, font, 1, 6, white, self.symbol)
        if market.has_data(self.symbol):
            self.closing_price = market.get_last_close_price(self.symbol)
            self.data = market.get_trading_day_data(self.symbol)
            self.graph.parse(self.data, self.closing_price)
            self.curr_price = market.get_current_price(self.symbol)
            self.curr_diff = market.get_current_difference(self.symbol)
            self.curr_percent = market.get_current_percent(self.symbol)
        
            graphics.DrawText(offscreen_canvas, font, 1, 13, grey, str(self.curr_price))
            line1_width = graphics.DrawText(_tmp_canvas, font, 0, 0, grey, str(self.curr_diff))
            line2_width = graphics.DrawText(_tmp_canvas, font, 0, 0, grey, str(self.curr_percent) + '%') 
            width = offscreen_canvas.width
            if self.curr_diff >= 0:
                graphics.DrawText(offscreen_canvas, font, width-line1_width, 6, green, str(self.curr_diff))
                graphics.DrawText(offscreen_canvas, font, width-line2_width, 13, green, str(self.curr_percent) + '%')
            else:
                graphics.DrawText(offscreen_canvas, font, width-line1_width, 6, red, str(self.curr_diff))
                graphics.DrawText(offscreen_canvas, font, width-line2_width, 13, red, str(self.curr_percent) + '%')

            self.graph.draw(offscreen_canvas, 0, 31)
        else:
            graphics.DrawText(offscreen_canvas, font, 1, 13, grey, "-.--")
            graphics.DrawText(offscreen_canvas, font, 50, 6, grey, "-.--")
            graphics.DrawText(offscreen_canvas, font, 45, 13, grey, "-.--%")

        lock.release()

        return offscreen_canvas

    class Graph:
        def __init__(self):
            self.height = 17
            self.width = 64
            self.open_time = 390 # minutes in stock day
            self.timestamps = list(numpy.rint(numpy.linspace(0,self.open_time-1,self.width)))
            self.data = list()

        def parse(self, raw, close_price):
            samples = list()
            for delta in self.timestamps:
                time = datetime.strptime(raw[-1]['datetime'], '%Y-%m-%d %H:%M:%S') + timedelta(minutes=delta)
                sample = list(filter(lambda values: values['datetime'] == time.strftime('%Y-%m-%d %H:%M:%S'), raw))
                if sample:
                    if delta == 0: # first data point is at open
                        samples.append(float(sample[0]['open']))
                    else:
                        samples.append(float(sample[0]['close']))
                else:
                    break

            max_val = max(samples)
            min_val = min(samples)
            
            if close_price > max_val:
                max_val = close_price
            elif close_price < min_val:
                min_val = close_price
            
            scale = self.height/(max_val-min_val)
            self.inflection_pt = round((close_price-min_val)*scale)

            x = 0
            self.data.clear()
            for sample in samples:
                self.data.append((x,int((sample-min_val)*scale)))
                x += 1
        
        def draw(self, canvas, x_offset, y_offset):
            green = (graphics.Color(0, 25, 0),graphics.Color(0, 255, 0))
            red = (graphics.Color(25, 0, 0), graphics.Color(255, 0, 0))
            # draw area
            for idx in range(0,len(self.data)):
                x = self.data[idx][0]
                y = self.data[idx][1]
                if y >= self.inflection_pt:
                    graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset, y_offset-self.inflection_pt, green[0])
                else:
                    graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset, y_offset-self.inflection_pt, red[0])
            # draw line
            for idx in range(0,len(self.data)):
                x = self.data[idx][0]
                y = self.data[idx][1]
                if y >= self.inflection_pt:
                    if x == len(self.data)-1:
                        graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset, y_offset-y, green[1])
                    else:
                        next_y = self.data[idx+1][1]
                        if next_y < self.inflection_pt:
                            graphics.DrawLine(canvas, x+x_offset, y_offset-self.inflection_pt, x+x_offset+1, y_offset-next_y, red[1])
                            graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset+1, y_offset-self.inflection_pt, green[1])
                        else:
                            graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset+1, y_offset-next_y, green[1])
                else:
                    if x == len(self.data)-1:
                        graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset, y_offset-y, red[1])
                    else:
                        next_y = self.data[idx+1][1]
                        if next_y >= self.inflection_pt:
                            graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset+1, y_offset-self.inflection_pt, red[1])
                            graphics.DrawLine(canvas, x+x_offset, y_offset-self.inflection_pt, x+x_offset+1, y_offset-next_y, green[1])
                        else:
                            graphics.DrawLine(canvas, x+x_offset, y_offset-y, x+x_offset+1, y_offset-next_y, red[1])
