#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import re
import tempfile
from pprint import pformat

logger = logging.getLogger(__name__)


class Direction:
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


class MessageTypes:
    NOTIFICATION = "notify"
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"


class BaseRemoteAccessor:
    def download(self):
        raise NotImplementedError


class DockerRemoteAccessor(BaseRemoteAccessor):
    def __init__(self, container_id):
        self.container_id = container_id

    def download(self, path):
        logger.debug("Downloading %s", path)
        path = path.replace("file://", "")
        filename, file_extension = os.path.splitext(path)
        _, temp = tempfile.mkstemp(suffix=file_extension)
        os.popen(f"docker cp {self.container_id}:{path} {temp}").read()
        return "file://" + temp


class Dispatcher:
    path_properties = {
        "newUri",
        "oldUri",
        "rootPath",
        "rootUri",
        "scopeUri",
        "targetUri",
        "uri",
    }

    def __init__(self, path_mapping, direction, remote_accessor=None):
        self.path_mapping = path_mapping
        self.direction = direction
        self.remote_accessor = remote_accessor
        self.rebuild_regex()

    def rebuild_regex(self):
        # Create a regular expression from all of the dictionary keys
        self.regex = re.compile("|".join(map(re.escape, self.path_mapping)))

    async def dispatch(self, payload):
        logger.debug("%s:\n%s", self.direction.upper(), pformat(payload))

        for obj, prop, path in self.find_path_properties(payload):
            new_path = self.multiple_replace(path)

            if not os.path.exists(new_path) and self.remote_accessor:
                new_path = self.remote_accessor.download(path)

            obj[prop] = new_path

        return payload

    def find_path_properties(self, dict_or_list):
        if isinstance(dict_or_list, dict):
            iterator = dict_or_list.items()
        else:
            iterator = enumerate(dict_or_list)

        for k, v in iterator:
            if k in self.path_properties:
                yield dict_or_list, k, v

            if isinstance(v, (dict, list)):
                yield from self.find_path_properties(v)

    def multiple_replace(self, text):
        # For each match, look up the corresponding value in the dictionary
        return self.regex.sub(lambda m: self.path_mapping[m.group(0)], text)


class LspProxyServer:
    def __init__(
        self,
        target_host,
        target_port,
        listen_host,
        listen_port,
        upstream_dispatcher,
        downstream_dispatcher,
        loop=None,
    ):
        self.upstream_dispatcher = upstream_dispatcher
        self.downstream_dispatcher = downstream_dispatcher

        self.target_host = target_host
        self.target_port = target_port

        self._loop = loop or asyncio.get_event_loop()

        self._server = asyncio.start_server(
            self.handle_connection, host=listen_host, port=listen_port
        )

    def start(self, and_loop=True):
        self._server = self._loop.run_until_complete(self._server)
        sockname = self._server.sockets[0].getsockname()
        logger.info("Listening established on %s", sockname)

        if and_loop:
            self._loop.run_forever()

    def stop(self, and_loop=True):
        self._server.close()

        if and_loop:
            self._loop.close()

    async def handle_connection(self, local_reader, local_writer):
        peername = local_writer.get_extra_info("peername")

        logger.info("Accepted connection from %s", peername)

        remote_reader, remote_writer = await asyncio.open_connection(
            host=self.target_host, port=self.target_port
        )

        await asyncio.gather(
            self.stream(local_reader, remote_writer, self.upstream_dispatcher),
            self.stream(remote_reader, local_writer, self.downstream_dispatcher),
        )

    async def stream(self, reader, writer, dispatcher):
        while not reader.at_eof():
            payload = await self.read_message(reader)

            if not payload:
                break

            payload = await dispatcher.dispatch(payload)

            await self.send_message(payload, writer)

    async def read_message(self, reader):
        """
        Example:

        Content-Length: ...\r\n
        \r\n
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "textDocument/didOpen",
            "params": {
                ...
            }
        }
        """

        headers = {}

        while True:
            data = await reader.readline()

            if not data:
                return None

            if data == b"\r\n":
                break

            name, _, value = data.decode().partition(":")
            headers[name.strip()] = value.strip()

        logger.debug(headers)

        content_length = int(headers["Content-Length"])
        body = await reader.read(content_length)
        return json.loads(body)

    async def send_message(self, payload, writer):
        body = json.dumps(payload)
        content_length = len(body.encode("utf-8"))
        response = f"Content-Length: {content_length}\r\n\r\n{body}"
        writer.write(response.encode("utf-8"))


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Proxy for Language Server running inside docker container"
    )

    parser.add_argument("--path-mapping", "-m", action="append")
    parser.add_argument("--target", "-t", required=True)
    parser.add_argument("--host", "-H", default="127.0.0.1")
    parser.add_argument("--port", "-p", type=int, required=True)
    parser.add_argument("--verbose", "-v", action="count", default=0)

    return parser.parse_args()


def setup_logging(args):
    if args.verbose == 0:
        level = logging.NOTSET
    elif args.verbose == 1:
        level = logging.WARNING
    elif args.verbose == 2:
        level = logging.INFO
    else:
        level = logging.DEBUG

    logger.setLevel(level)
    logger.addHandler(logging.StreamHandler())


def parse_path_mapping(args):
    upstream = dict([mapping.split(":") for mapping in args.path_mapping])
    downstream = {v: k for k, v in upstream.items()}
    return upstream, downstream


def parse_target(args):
    dest_host, _, dest_port = args.target.rpartition(":")
    return dest_host, int(dest_port)


def main():
    args = parse_args()
    setup_logging(args)

    target_host, target_port = parse_target(args)
    upstream_mapping, downstream_mapping = parse_path_mapping(args)

    proxy = LspProxyServer(
        target_host=target_host,
        target_port=target_port,
        listen_host=args.host,
        listen_port=args.port,
        upstream_dispatcher=Dispatcher(
            path_mapping=upstream_mapping, direction=Direction.UPSTREAM
        ),
        downstream_dispatcher=Dispatcher(
            path_mapping=downstream_mapping, direction=Direction.DOWNSTREAM
        ),
    )

    try:
        proxy.start()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()


if __name__ == "__main__":
    main()
