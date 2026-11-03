[app]
title = Magic Pump 24/7
package.name = magicpump
package.domain = com.eddy
p4a.branch = master
source.dir = .
source.include_exts = py,json,png,jpg,kv
version = 1.3.4
icon.filename = icon.png

# API 34 exige declarar un tipo para el servicio en primer plano.
# dataSync evita el límite de seis horas aplicable a apps que apuntan a API 35+;
# HyperOS aún puede detener procesos por batería o cierre forzado.
services = bot:bot_service.py:foreground:sticky:foregroundServiceType=dataSync

requirements = python3==3.11.8,hostpython3==3.11.8,kivy,requests,android
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,WAKE_LOCK,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,POST_NOTIFICATIONS
android.api = 34
android.minapi = 24
android.ndk = 28c
android.archs = arm64-v8a
android.private_storage = True
p4a.bootstrap = sdl2

[buildozer]
log_level = 2
warn_on_root = 1
