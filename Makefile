SHELL := /bin/zsh

apply:
	./scripts/apply.sh

apply-system:
	sudo /Users/copilot/ig88-host/scripts/apply-system.sh

status:
	@UID_NUM=$$(id -u); \
	launchctl print gui/$$UID_NUM/com.syntax922.ig88.lmstudio || true; \
	launchctl print gui/$$UID_NUM/com.syntax922.ig88.caddy || true; \
	launchctl print gui/$$UID_NUM/com.syntax922.ig88.gitops || true; \
	launchctl print system/com.syntax922.ig88.lmstudio || true; \
	launchctl print system/com.syntax922.ig88.caddy || true; \
	launchctl print system/com.syntax922.ig88.gitops || true

logs:
	@tail -n 200 ./logs/lmstudio.log ./logs/caddy.log ./logs/gitops.log ./logs/caddy-access.log 2>/dev/null || true
