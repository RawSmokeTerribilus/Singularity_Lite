FROM alpine:latest

# add mono repo and mono
RUN apk add --no-cache mono --repository http://dl-cdn.alpinelinux.org/alpine/edge/testing

# install requirements
# libarchive-tools trae bsdtar: listar lo que hay dentro de un .7z/.cab para
# saber de qué clase es el envase. Opcional -- sin él se le pregunta al
# operador -- pero es una palabra.
RUN apk add --no-cache --upgrade ffmpeg mediainfo python3 git py3-pip python3-dev python3-tkinter g++ cargo mktorrent rust libarchive-tools

# create virtual environment
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# change workdir
WORKDIR /app

# install reqs
COPY requirements.txt .
RUN pip3 install -r requirements.txt

# copy everything
COPY . .

ENTRYPOINT ["python3", "/Uploadrr/upload.py"]
