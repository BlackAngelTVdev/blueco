SHELL := /bin/bash
PYTHON ?= python3

.PHONY: help test selftest run devices init android android-release linux linux-deps linux-pkg fmt clean

help:
	@echo "blueco - cibles disponibles"
	@echo
	@echo "  make test            tests unitaires et d'intégration (socket locale)"
	@echo "  make selftest        auto-test de bout en bout, backend audio simulé"
	@echo "  make run             démarre le démon (serve) en mode verbeux"
	@echo "  make devices         liste les appareils connus de BlueZ"
	@echo "  make init            écrit ~/.config/blueco/config.json"
	@echo "  make linux           install Arch Linux (lanceur + unités systemd)"
	@echo "  make linux-deps      installe les paquets Arch nécessaires (sudo)"
	@echo "  make linux-pkg       construit et installe le paquet pacman"
	@echo "  make android         compile l'APK de debug (scripts/build-android.sh)"
	@echo "  make android-release compile l'APK de release"
	@echo "  make clean           supprime les artefacts de build"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

selftest:
	$(PYTHON) -m blueco --fake selftest

run:
	$(PYTHON) -m blueco serve -v

devices:
	$(PYTHON) -m blueco devices

init:
	$(PYTHON) -m blueco init

linux:
	./scripts/build-linux.sh

linux-deps:
	./scripts/build-linux.sh --deps

linux-pkg:
	./scripts/build-linux.sh --pkg

android:
	./scripts/build-android.sh

android-release:
	./scripts/build-android.sh --release

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf dist android/app/build .gradle
