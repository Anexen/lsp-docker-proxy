#!/usr/bin/env python3

import asyncio
import json
import logging
import re
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


class BaseDispatcher:
    def __init__(self, path_mapping):
        self.path_mapping = path_mapping or {}
        self.reversed_path_mapping = {v: k for k, v in path_mapping.items()}

    async def dispatch(self, payload, direction):
        raise NotImplementedError

    def get_message_type(self, payload):
        if "method" in payload:
            return (
                MessageTypes.REQUEST if "id" in payload else MessageTypes.NOTIFICATION
            )

        if "result" in payload:
            return MessageTypes.RESPONSE

        if "error" in payload:
            return MessageTypes.ERROR

        raise NotImplementedError("Unknown message type")


class NaiveDispatcher(BaseDispatcher):
    """
    Performs dumb replace by defined mapping
    """

    async def dispatch(self, payload, direction):
        if direction == Direction.UPSTREAM:
            mapping = self.path_mapping
        else:
            mapping = self.reversed_path_mapping

        return json.loads(self.multiple_replace(mapping, json.dumps(payload)))

    @staticmethod
    def multiple_replace(adict, text):
        # Create a regular expression from all of the dictionary keys
        regex = re.compile("|".join(map(re.escape, adict.keys())))

        # For each match, look up the corresponding value in the dictionary
        return regex.sub(lambda match: adict[match.group(0)], text)


class SmartDispatcher(BaseDispatcher):
    """
    WIP

    Knows how to handle concrete methods.
    Can download unknown files from the remote host.
    """

    def __init__(self, path_mapping):
        super().__init__(path_mapping)

        self._requests_in_progress = {}

    async def dispatch(self, payload, direction):
        message_type = self.get_message_type(payload)

        if message_type == MessageTypes.ERROR:
            logger.error(
                "%s (%s)", payload["error"]["message"], payload["error"]["code"]
            )
            return payload

        if message_type in MessageTypes.REQUEST:
            method = payload["method"]
            self._requests_in_progress[payload["id"]] = payload["method"]
        elif message_type == MessageTypes.RESPONSE:
            method = self._requests_in_progress[payload["id"]]
            del self._requests_in_progress[payload["id"]]
        elif message_type == MessageTypes.NOTIFICATION:
            method = payload["method"]

        handler_name = method.replace("/", "_") + "__" + message_type

        handler = getattr(self, handler_name, None)

        if handler is not None:
            return handler(payload)

        return payload


class LspProxyServer:
    def __init__(self, dest_host, dest_port, listen_port, dispatcher, loop=None):
        self.dispatcher = dispatcher

        self.dest_host = dest_host
        self.dest_port = dest_port

        self._loop = loop or asyncio.get_event_loop()

        self._server = asyncio.start_server(
            self.handle_connection, host="0.0.0.0", port=listen_port
        )

    def start(self, and_loop=True):
        self._server = self._loop.run_until_complete(self._server)
        sockname = self._server.sockets[0].getsockname()
        logger.info("Listening established on {0}".format(sockname))

        if and_loop:
            self._loop.run_forever()

    def stop(self, and_loop=True):
        self._server.close()

        if and_loop:
            self._loop.close()

    async def handle_connection(self, local_reader, local_writer):
        peername = local_writer.get_extra_info("peername")

        logger.info("Accepted connection from {}".format(peername))

        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                host=self.dest_host, port=self.dest_port
            )

            await asyncio.gather(
                self.upstream(local_reader, remote_writer),
                self.downstream(remote_reader, local_writer),
            )
        finally:
            local_writer.close()

    async def upstream(self, local_reader, remote_writer):
        try:
            while not local_reader.at_eof():
                payload = await self.read_message(local_reader)

                if not payload:
                    break

                payload = await self.dispatcher.dispatch(payload, Direction.UPSTREAM)

                logger.debug("UPSTREAM:\n%s", pformat(payload))

                await self.send_message(payload, remote_writer)
        finally:
            remote_writer.close()

    async def downstream(self, remote_reader, local_writer):
        try:
            while not remote_reader.at_eof():
                payload = await self.read_message(remote_reader)

                if not payload:
                    break

                payload = await self.dispatcher.dispatch(payload, Direction.DOWNSTREAM)

                logger.debug("DOWNSTREAM:\n%s", pformat(payload))

                await self.send_message(payload, local_writer)
        finally:
            local_writer.close()

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

        data = await reader.readline()

        if not data:
            return None

        content_length_header = data.decode().rstrip()
        content_length = int(content_length_header[16:])

        # read optional content type header
        content_type_header = await reader.readline()
        content_type = content_type_header.decode().rstrip()

        if content_type:
            await reader.readline()  # read empty line

        body = await reader.read(content_length)
        return json.loads(body)

    async def send_message(self, payload, writer):
        body = json.dumps(payload)
        content_length = len(body.encode("utf-8"))
        response = f"Content-Length: {content_length}\r\n\r\n{body}"
        writer.write(response.encode("utf-8"))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Proxy for Language Server running inside docker container"
    )
    parser.add_argument("--path-mapping", "-m", action="append")
    parser.add_argument("--target", "-t", required=True)
    parser.add_argument("--port", "-p", type=int, required=True)

    parser.add_argument("--verbose", "-v", action="count", default=0)

    args = parser.parse_args()

    logging_level = {
        0: logging.NOTSET,
        1: logging.ERROR,
        2: logging.INFO,
        3: logging.DEBUG,
    }

    logger.setLevel(logging_level[args.verbose])
    logger.addHandler(logging.StreamHandler())

    path_mapping = dict([mapping.split(":") for mapping in args.path_mapping])

    logger.debug("Path mapping: %s", pformat(path_mapping))

    dispatcher = NaiveDispatcher(path_mapping=path_mapping)

    dest_host, _, dest_port = args.target.rpartition(":")

    proxy = LspProxyServer(
        dest_host=dest_host,
        dest_port=int(dest_port),
        listen_port=args.port,
        dispatcher=dispatcher,
    )

    try:
        proxy.start()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
