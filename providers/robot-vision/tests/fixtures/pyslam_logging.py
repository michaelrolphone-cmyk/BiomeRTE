"""
* This file is part of PYSLAM
*
* Copyright (C) 2016-present Luigi Freda <luigi dot freda at gmail dot com>
*
* PYSLAM is free software: you can redistribute it and/or modify
* it under the terms of the GNU General Public License as published by
* the Free Software Foundation, either version 3 of the License, or
* (at your option) any later version.
*
* PYSLAM is distributed in the hope that it will be useful,
* but WITHOUT ANY WARRANTY; without even the implied warranty of
* MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
* GNU General Public License for more details.
*
* You should have received a copy of the GNU General Public License
* along with PYSLAM. If not, see <http://www.gnu.org/licenses/>.
"""

# Logging class excerpt from luigifreda/pyslam at
# a4e6a31f211ac6b2f9ecf4ab628734f6194caa15 (utilities/logging.py).
# Kept unmodified so regression tests exercise the real pinned factory.
import logging
import os
import threading

class Logging(object):
    """
    A class for logging to multiple files, streams, etc.
    Example:
    # first file logger
    logger = Logging.setup_file_logger('first_logger', 'first_logfile.log')
    logger.info('This is just info message')

    # second file logger
    super_logger = Logging.setup_file_logger('second_logger', 'second_logfile.log')
    super_logger.error('This is an error message')
    """

    time_log_formatter = logging.Formatter("%(levelname)s[%(asctime)s] %(message)s")
    notime_log_formatter = logging.Formatter("%(levelname)s %(message)s")
    simple_log_formatter = logging.Formatter("%(message)s")
    thread_log_formatter = logging.Formatter("%(levelname)s] (%(threadName)-10s) %(message)s")

    @staticmethod
    def setup_logger(name, level=logging.INFO, formatter=time_log_formatter):  # to sys.stderr
        """To setup as many loggers as you want with a selected formatter"""
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.addHandler(handler)
        return logger

    @staticmethod
    def setup_file_logger(
        name, log_file, level=logging.INFO, mode="+w", formatter=time_log_formatter
    ):  # to file
        """To setup as many loggers as you want with a selected formatter"""
        if not os.path.exists(os.path.dirname(log_file)):
            os.makedirs(os.path.dirname(log_file))
        handler = logging.FileHandler(log_file, mode=mode)
        handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.addHandler(handler)
        return logger

    @staticmethod
    def setup_socket_logger(
        name, host, port, level=logging.INFO, formatter=time_log_formatter
    ):  # TCP-IP
        """To setup as many loggers as you want with a selected formatter"""
        handler = logging.SocketHandler(host, port)
        handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.addHandler(handler)
        return logger

    @staticmethod
    def setup_udp_logger(name, host, port, level=logging.INFO, formatter=time_log_formatter):  # UDP
        """To setup as many loggers as you want with a selected formatter"""
        handler = logging.DatagramHandler(host, port)
        handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.addHandler(handler)
        return logger
