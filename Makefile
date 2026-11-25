# ffa — run the web app as a systemd user service. Everything else goes through uv
# (see CLAUDE.md).

UV       ?= $(shell command -v uv)
UNIT_DIR  = $(or $(XDG_CONFIG_HOME),$(HOME)/.config)/systemd/user

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: install-service
install-service: ## Run `ffa serve` from this clone as a systemd user service
	@test -n "$(UV)" || { echo "uv not found on PATH; set UV=/path/to/uv"; exit 1; }
	mkdir -p $(UNIT_DIR)
	sed -e 's|@DIR@|$(CURDIR)|' -e 's|@UV@|$(UV)|' contrib/systemd/ffa.service > $(UNIT_DIR)/ffa.service
	systemctl --user daemon-reload
	systemctl --user enable ffa.service
	systemctl --user restart ffa.service

.PHONY: uninstall-service
uninstall-service: ## Stop and remove the systemd user service
	-systemctl --user disable --now ffa.service
	rm -f $(UNIT_DIR)/ffa.service
	systemctl --user daemon-reload
