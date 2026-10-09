INSTALL_LIB := $(HOME)/.local/share/prompt-mesh
INSTALL_BIN := $(HOME)/.local/bin
INSTALL_SYSTEMD := $(HOME)/.config/systemd/user

.PHONY: all install uninstall test

all: test

test:
	python3 src/shm_reader.py
	python3 src/pruner.py
	python3 src/router.py
	python3 src/circuit.py

install:
	mkdir -p $(INSTALL_LIB) $(INSTALL_BIN) $(INSTALL_SYSTEMD)
	install -m 644 src/*.py $(INSTALL_LIB)/
	@echo '#!/usr/bin/env bash' > $(INSTALL_BIN)/prompt-mesh
	@echo 'export PYTHONPATH="$(INSTALL_LIB):$$PYTHONPATH"' >> $(INSTALL_BIN)/prompt-mesh
	@echo 'exec /usr/bin/python3 "$(INSTALL_LIB)/gateway.py" "$$@"' >> $(INSTALL_BIN)/prompt-mesh
	chmod 755 $(INSTALL_BIN)/prompt-mesh
	install -m 644 systemd/prompt-mesh.service $(INSTALL_SYSTEMD)/prompt-mesh.service
	systemctl --user daemon-reload

uninstall:
	-systemctl --user stop prompt-mesh.service 2>/dev/null || true
	-systemctl --user disable prompt-mesh.service 2>/dev/null || true
	rm -rf $(INSTALL_LIB)
	rm -f $(INSTALL_BIN)/prompt-mesh
	rm -f $(INSTALL_SYSTEMD)/prompt-mesh.service
	systemctl --user daemon-reload
