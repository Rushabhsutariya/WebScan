# -*- coding: utf-8 -*-
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#  Author: Mauro Soria

import gc
import os
import sys
import time
import re
import urllib.parse
from threading import Lock

from queue import Queue

from lib.connection import Requester, RequestException
from lib.core import Dictionary, Fuzzer, ReportManager
from lib.reports import JSONReport, PlainTextReport, SimpleReport
from lib.utils import FileUtils


class SkipTargetInterrupt(Exception):
    pass


MAYOR_VERSION = 0
MINOR_VERSION = 3
REVISION = 8
VERSION = {
    "MAYOR_VERSION": MAYOR_VERSION,
    "MINOR_VERSION": MINOR_VERSION,
    "REVISION": REVISION
}


class Controller(object):
    def __init__(self, script_path, arguments, output):
        global VERSION
        program_banner = open(FileUtils.build_path(script_path, "lib", "controller", "banner.txt")).read().format(
            **VERSION)

        self.script_path = script_path
        self.exit = False
        self.arguments = arguments
        self.output = output
        self.savePath = self.script_path
        self.doneDirs = []

        self.recursive_level_max = self.arguments.recursive_level_max

        if self.arguments.httpmethod.lower() not in ["get", "head", "post"]:
            self.output.error("Inavlid http method!")
            exit(1)

        self.httpmethod = self.arguments.httpmethod.lower()

        if self.arguments.saveHome:
            savePath = self.get_save_path()

            if not FileUtils.exists(savePath):
                FileUtils.create_directory(savePath)

            if FileUtils.exists(savePath) and not FileUtils.is_dir(savePath):
                self.output.error('Cannot use {} because is a file. Should be a directory'.format(savePath))
                exit(1)

            if not FileUtils.can_write(savePath):
                self.output.error('Directory {} is not writable'.format(savePath))
                exit(1)

            logs = FileUtils.build_path(savePath, "logs")

            if not FileUtils.exists(logs):
                FileUtils.create_directory(logs)

            reports = FileUtils.build_path(savePath, "reports")

            if not FileUtils.exists(reports):
                FileUtils.create_directory(reports)

            self.savePath = savePath

        self.reportsPath = FileUtils.build_path(self.savePath, "logs")
        self.blacklists = self.get_blacklists()
        self.fuzzer = None
        self.excludeStatusCodes = self.arguments.excludeStatusCodes
        self.excludeTexts = self.arguments.excludeTexts
        self.excludeRegexps = self.arguments.excludeRegexps
        self.recursive = self.arguments.recursive
        self.suppressEmpty = self.arguments.suppressEmpty
        self.directories = Queue()
        self.excludeSubdirs = (arguments.excludeSubdirs if arguments.excludeSubdirs is not None else [])
        self.output.header(program_banner)
        self.dictionary = Dictionary(self.arguments.wordlist, self.arguments.extensions,
                                     self.arguments.lowercase, self.arguments.forceExtensions)
        self.print_config()
        self.errorLog = None
        self.errorLogPath = None
        self.errorLogLock = Lock()
        self.batch = False
        self.batchSession = None
        self.setup_error_logs()
        self.output.new_line("\nError Log: {0}".format(self.errorLogPath))

        if self.arguments.autoSave and len(self.arguments.urlList) > 1:
            self.setup_batch_reports()
            self.output.new_line("\nAutoSave path: {0}".format(self.batchDirectoryPath))

        if self.arguments.useRandomAgents:
            self.randomAgents = FileUtils.get_lines(FileUtils.build_path(script_path, "db", "user-agents.txt"))

        try:
            for url in self.arguments.urlList:

                try:
                    gc.collect()
                    self.reportManager = ReportManager()
                    self.currentUrl = url
                    self.output.target(self.currentUrl)

                    try:
                        self.requester = Requester(url, cookie=self.arguments.cookie,
                                                   useragent=self.arguments.useragent,
                                                   maxPool=self.arguments.threadsCount,
                                                   maxRetries=self.arguments.maxRetries, delay=self.arguments.delay,
                                                   timeout=self.arguments.timeout,
                                                   ip=self.arguments.ip, proxy=self.arguments.proxy,
                                                   redirect=self.arguments.redirect,
                                                   requestByHostname=self.arguments.requestByHostname,
                                                   httpmethod=self.httpmethod)
                        self.requester.request("/")

                    except RequestException as e:
                        self.output.error(e.args[0]['message'])
                        raise SkipTargetInterrupt

                    if self.arguments.useRandomAgents:
                        self.requester.set_random_agents(self.randomAgents)

                    for key, value in arguments.headers.items():
                        self.requester.set_header(key, value)

                    # Initialize directories Queue with start Path
                    self.basePath = self.requester.basePath

                    if self.arguments.scanSubdirs is not None:
                        for subdir in self.arguments.scanSubdirs:
                            self.directories.put(subdir)

                    else:
                        self.directories.put('')

                    self.setup_reports(self.requester)

                    matchCallbacks = [self.match_callback]
                    notFoundCallbacks = [self.not_found_callback]
                    errorCallbacks = [self.error_callback, self.append_error_log]

                    self.fuzzer = Fuzzer(self.requester, self.dictionary, test_fail_path=self.arguments.testFailPath,
                                         threads=self.arguments.threadsCount, match_callbacks=matchCallbacks,
                                         not_found_callbacks=notFoundCallbacks, error_callbacks=errorCallbacks)
                    try:
                        self.wait()
                    except RequestException as e:
                        self.output.error("Fatal error during site scanning: " + e.args[0]['message'])
                        raise SkipTargetInterrupt

                except SkipTargetInterrupt:
                    continue

                finally:
                    self.reportManager.save()

        except KeyboardInterrupt:
            self.output.error('\nCanceled by the user')
            exit(0)

        finally:
            if not self.errorLog.closed:
                self.errorLog.close()

            self.reportManager.close()

        self.output.warning('\nTask Completed')

    def print_config(self):
        self.output.config(
            ', '.join(self.arguments.extensions),
            str(self.arguments.threadsCount),
            str(len(self.dictionary)),
            str(self.httpmethod),
            self.recursive,
            str(self.recursive_level_max)
        )

    def get_save_path(self):
        basePath = None
        dirPath = None
        basePath = os.path.expanduser('~')

        if os.name == 'nt':
            dirPath = "dirsearch"
        else:
            dirPath = ".dirsearch"

        return FileUtils.build_path(basePath, dirPath)

    def get_blacklists(self):
        blacklists = {}

        for status in [400, 403, 500]:
            blacklistFileName = FileUtils.build_path(self.script_path, 'db')
            blacklistFileName = FileUtils.build_path(blacklistFileName, '{}_blacklist.txt'.format(status))

            if not FileUtils.can_read(blacklistFileName):
                # Skip if cannot read file
                continue

            blacklists[status] = []

            for line in FileUtils.get_lines(blacklistFileName):
                # Skip comments
                if line.lstrip().startswith('#'):
                    continue

                blacklists[status].append(line)

        return blacklists

    def setup_error_logs(self):
        fileName = "errors-{0}.log".format(time.strftime('%y-%m-%d_%H-%M-%S'))
        self.errorLogPath = FileUtils.build_path(FileUtils.build_path(self.savePath, "logs", fileName))
        self.errorLog = open(self.errorLogPath, "w")

    def setup_batch_reports(self):
        self.batch = True
        self.batchSession = "BATCH-{0}".format(time.strftime('%y-%m-%d_%H-%M-%S'))
        self.batchDirectoryPath = FileUtils.build_path(self.savePath, "reports", self.batchSession)

        if not FileUtils.exists(self.batchDirectoryPath):
            FileUtils.create_directory(self.batchDirectoryPath)

            if not FileUtils.exists(self.batchDirectoryPath):
                self.output.error("Couldn't create batch folder {}".format(self.batchDirectoryPath))
                sys.exit(1)

        if FileUtils.can_write(self.batchDirectoryPath):
            FileUtils.create_directory(self.batchDirectoryPath)
            targetsFile = FileUtils.build_path(self.batchDirectoryPath, "TARGETS.txt")
            FileUtils.write_lines(targetsFile, self.arguments.urlList)

        else:
            self.output.error("Couldn't create batch folder {}.".format(self.batchDirectoryPath))
            sys.exit(1)

    def setup_reports(self, requester):
        if self.arguments.autoSave:
            basePath = ('/' if requester.basePath is '' else requester.basePath)
            basePath = basePath.replace(os.path.sep, '.')[1:-1]
            fileName = None
            directoryPath = None

            if self.batch:
                fileName = requester.host
                directoryPath = self.batchDirectoryPath

            else:
                fileName = ('{}_'.format(basePath) if basePath is not '' else '')
                fileName += time.strftime('%y-%m-%d_%H-%M-%S')
                directoryPath = FileUtils.build_path(self.savePath, 'reports', requester.host)

            outputFile = FileUtils.build_path(directoryPath, fileName)

            if FileUtils.exists(outputFile):
                i = 2

                while FileUtils.exists(outputFile + "_" + str(i)):
                    i += 1

                outputFile += "_" + str(i)

            if not FileUtils.exists(directoryPath):
                FileUtils.create_directory(directoryPath)

                if not FileUtils.exists(directoryPath):
                    self.output.error("Couldn't create reports folder {}".format(directoryPath))
                    sys.exit(1)
            if FileUtils.can_write(directoryPath):
                report = None

                if self.arguments.autoSaveFormat == 'simple':
                    report = SimpleReport(requester.host, requester.port, requester.protocol, requester.basePath,
                                          outputFile)
                if self.arguments.autoSaveFormat == 'json':
                    report = JSONReport(requester.host, requester.port, requester.protocol, requester.basePath,
                                        outputFile)
                else:
                    report = PlainTextReport(requester.host, requester.port, requester.protocol, requester.basePath,
                                             outputFile)

                self.reportManager.add_output(report)

            else:
                self.output.error("Can't write reports to {}".format(directoryPath))
                sys.exit(1)

        if self.arguments.simpleOutputFile is not None:
            self.reportManager.add_output(SimpleReport(requester.host, requester.port, requester.protocol,
                                                       requester.basePath, self.arguments.simpleOutputFile))

        if self.arguments.plainTextOutputFile is not None:
            self.reportManager.add_output(PlainTextReport(requester.host, requester.port, requester.protocol,
                                                          requester.basePath, self.arguments.plainTextOutputFile))

        if self.arguments.jsonOutputFile is not None:
            self.reportManager.add_output(JSONReport(requester.host, requester.port, requester.protocol,
                                                     requester.basePath, self.arguments.jsonOutputFile))

    def match_callback(self, path):
        self.index += 1

        if path.status is not None:
            if path.status not in self.excludeStatusCodes and (
                    self.blacklists.get(path.status) is None or path.path not in self.blacklists.get(
                        path.status)) and not (
                            self.suppressEmpty and (len(path.response.body) == 0)):

                for excludeText in self.excludeTexts:
                    if excludeText in path.response.body.decode():
                        del path
                        return

                for excludeRegexp in self.excludeRegexps:
                    if re.search(excludeRegexp, path.response.body.decode()) is not None:
                        del path
                        return

                self.output.status_report(path.path, path.response)
                if path.response.redirect:
                    self.add_redirect_directory(path)
                else:
                    self.add_directory(path.path)
                self.reportManager.add_path(self.currentDirectory + path.path, path.status, path.response)
                self.reportManager.save()
                del path

    def not_found_callback(self, path):
        self.index += 1
        self.output.lastPath(path, self.index, len(self.dictionary))
        del path

    def error_callback(self, path, errorMsg):
        self.output.add_connection_error()
        del path

    def append_error_log(self, path, errorMsg):
        with self.errorLogLock:
            line = time.strftime('[%y-%m-%d %H:%M:%S] - ')
            line += self.currentUrl + " - " + path + " - " + errorMsg
            self.errorLog.write(os.linesep + line)
            self.errorLog.flush()

    def handle_interrupt(self):
        self.output.warning('CTRL+C detected: Pausing threads, please wait...')
        self.fuzzer.pause()

        try:
            while True:
                msg = "[e]xit / [c]ontinue"

                if not self.directories.empty():
                    msg += " / [n]ext"

                if len(self.arguments.urlList) > 1:
                    msg += " / [s]kip target"

                self.output.in_line(msg + ': ')

                option = input()

                if option.lower() == 'e':
                    self.exit = True
                    self.fuzzer.stop()
                    raise KeyboardInterrupt

                elif option.lower() == 'c':
                    self.fuzzer.play()
                    return

                elif not self.directories.empty() and option.lower() == 'n':
                    self.fuzzer.stop()
                    return

                elif len(self.arguments.urlList) > 1 and option.lower() == 's':
                    raise SkipTargetInterrupt

                else:
                    continue

        except KeyboardInterrupt as SystemExit:
            self.exit = True
            raise KeyboardInterrupt

    def process_paths(self):
        while True:
            try:
                while not self.fuzzer.wait(0.3):
                    continue
                break

            except (KeyboardInterrupt, SystemExit) as e:
                self.handle_interrupt()

    def wait(self):
        while not self.directories.empty():
            self.index = 0
            self.currentDirectory = self.directories.get()
            self.output.warning('[{1}] Starting: {0}'.format(self.currentDirectory, time.strftime('%H:%M:%S')))
            self.fuzzer.requester.basePath = self.basePath + self.currentDirectory
            self.output.basePath = self.basePath + self.currentDirectory
            self.fuzzer.start()
            self.process_paths()
        return

    def add_directory(self, path):
        if not self.recursive:
            return False

        if path.endswith('/'):
            if path in [directory + '/' for directory in self.excludeSubdirs]:
                return False

            dir = self.currentDirectory + path

            if dir in self.doneDirs:
                return False

            if dir.count("/") > self.recursive_level_max:
                return False

            self.directories.put(dir)

            self.doneDirs.append(dir)

            return True

        else:
            return False

    def add_redirect_directory(self, path):
        """Resolve the redirect header relative to the current URL and add the
        path to self.directories if it is a subdirectory of the current URL."""
        if not self.recursive:
            return False

        baseUrl = self.currentUrl.rstrip("/") + "/" + self.currentDirectory
        absoluteUrl = urllib.parse.urljoin(baseUrl, path.response.redirect)
        if absoluteUrl.startswith(baseUrl) and absoluteUrl != baseUrl and absoluteUrl.endswith("/"):
            dir = absoluteUrl[len(baseUrl):]

            if dir in self.doneDirs:
                return False

            if dir.count("/") > self.recursive_level_max:
                return False

            self.directories.put(dir)

            self.doneDirs.append(dir)

            return True

        return False
