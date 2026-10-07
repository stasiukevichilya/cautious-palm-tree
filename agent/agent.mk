# Open WebUI and helpers (included from ../Makefile; recipes run from the repository root).
# GID of the Docker socket, so the agent terminal (Open WebUI) can drive the host Docker.
export AGENT_DOCKER_GID ?= $(shell stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)

.PHONY: agent agent-key agent-build agent-skills agent-functions agent-stop logs-agent blender-addon

help-agent:
	@echo "== Агент (agent/)"
	@echo "make agent       - Open WebUI в локальной сети :1098 + терминал со скиллами + MCP images; модель поднимать отдельно"
	@echo "make agent-key / agent-build / agent-skills / agent-functions / agent-stop / logs-agent"
	@echo "make blender-addon - поставить аддон blender-mcp в Blender на Windows (BLENDER_VERSION=4.0)"

# Open WebUI + Open Terminal + MCP images: not models, so model switching and models-stop leave them running,
# except images / images-uc, which only front the image generators and follow them (images-sync).
AGENT_SERVICES := agent-webui agent-terminal $(IMAGES_SERVICES) blender-mcp bili headroom
BLENDER_VERSION ?= 4.0
# Skill directories mounted into the terminal; missing ones are created so the bind mounts work.
AGENT_SKILL_DIRS := $(HOME)/.claude/skills $(HOME)/.agents/skills $(HOME)/.config/opencode/skills \
  $(HOME)/.pi/skills $(HOME)/.pi/agent/skills $(HOME)/.pi/agent/npm/node_modules

agent-key:
	@touch .env
	@[ ! -s .env ] || [ -z "$$(tail -c1 .env)" ] || echo >> .env
	@for name in AGENT_SECRET_KEY AGENT_TERMINAL_KEY AGENT_ADMIN_PASSWORD; do \
	  grep -q "^$$name=" .env && echo "$$name already set in .env" || \
	  { printf '%s=%s\n' "$$name" "$$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> .env; \
	    echo "$$name added to .env"; }; done
	@grep -q '^AGENT_ADMIN_EMAIL=' .env || { echo 'AGENT_ADMIN_EMAIL=admin@ml.local' >> .env; echo "AGENT_ADMIN_EMAIL added to .env"; }

agent-build:
	$(COMPOSE) --profile agent build images blender-mcp bili headroom
	$(COMPOSE) --profile agent pull agent-webui agent-terminal

agent: infra
	@grep -q '^TGBOT_ADMIN_KEY=.\{16,\}' .env || { echo "Add TGBOT_ADMIN_KEY to .env: make tgbot-key"; exit 1; }
	@for name in AGENT_SECRET_KEY AGENT_TERMINAL_KEY AGENT_ADMIN_EMAIL AGENT_ADMIN_PASSWORD; do grep -q "^$$name=." .env || { echo "Add $$name to .env: make agent-key"; exit 1; }; done
	mkdir -p outputs/agent/webui outputs/agent/terminal outputs/agent/bili outputs/agent/headroom $(AGENT_SKILL_DIRS)
	$(COMPOSE) --profile agent up -d --no-deps $(filter-out $(IMAGES_SERVICES),$(AGENT_SERVICES))
	@$(MAKE) --no-print-directory images-sync
	@echo "Open WebUI: http://$$(hostname -I | awk '{print $$1}'):1098 (вход: AGENT_ADMIN_EMAIL / AGENT_ADMIN_PASSWORD из .env)"

# Open WebUI filters from agent/functions: Billion context and Headroom toggles, Context usage (run once, and after edits).
agent-functions:
	$(COMPOSE) --profile agent exec agent-webui python3 /opt/agent/functions/install.py /opt/agent/functions

# Re-link the host skills after installing or removing some (restarts the terminal only).
agent-skills:
	$(COMPOSE) --profile agent restart agent-terminal
	$(COMPOSE) --profile agent logs --no-log-prefix agent-terminal | grep -E '^skill|skills linked' | tail -60

agent-stop:
	$(COMPOSE) --profile agent stop $(AGENT_SERVICES)

logs-agent:
	$(COMPOSE) --profile agent logs --tail=100 -f $(AGENT_SERVICES)

# The addon file comes from the blender-mcp image, so addon and server versions always match.
blender-addon:
	@appdata=$$(wslpath "$$(powershell.exe -NoProfile -Command '$$env:APPDATA' | tr -d '\r')") && \
	  dir="$$appdata/Blender Foundation/Blender/$(BLENDER_VERSION)/scripts/addons" && mkdir -p "$$dir" && \
	  docker run --rm --entrypoint python local/ml-blender-mcp:1 -c \
	    "import blender_mcp, pathlib; print((pathlib.Path(blender_mcp.__file__).parent / 'bundled' / 'addon.py').read_text(), end='')" \
	    > "$$dir/blender_mcp.py" && echo "Addon: $$dir/blender_mcp.py"
	@echo "Blender: Edit > Preferences > Add-ons > включить 'MCP for Blender'; 3D View > N > MCP for Blender > Start"
