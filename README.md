# lsp-docker-proxy

[![License](https://img.shields.io/github/license/mashape/apistatus.svg)](https://github.com/Anexen/lsp-docker-proxy/blob/master/LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Proxy for Language Server running inside docker container

# Requirements

* python 3.6

# Usage

Start docker container

```
$ docker run -p 9999:9999 -v $(pwd):/app <dev-image> pyls --tcp --host 0.0.0.0 --port 9999
```

Run proxy

```
$ python3 proxy.py
    -m ~/projects/local:/app \  # don't use trailing slashes
    -m ~/projects/pseudo-venv/name:/usr/local/lib/python3.6/site-packages \
    --target localhost:9999 \
    --port 10001
```

Use proxy address in your editor. Example for Neovim with [LanguageClient-neovim](https://github.com/autozimu/LanguageClient-neovim) plugin:

```
let g:LanguageClient_serverCommands = {
    \ 'python': ['tcp://127.0.0.1:10001'],
    \ 'javascript': ['tcp://127.0.0.1:11011'],
    \ }
```

# TODO

* support python 3.7, 3.8
* optional ujson dependency
* download remote files if not exists on host
