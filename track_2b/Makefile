.PHONY: run onprem model eval score test pdf clean

# Pinned community GGUF of Apertus 1.5 8B (text-only conversion, Q4_K_M, Apache-2.0).
MODEL_REPO := Colby/apertus-v1.5-8b-text-Q4_K_M-GGUF
MODEL_REV  := 248ec68a63e219e3f061e4c946fc8a20d63b9975
MODEL_FILE := apertus-v1.5-8b-text-q4_k_m.gguf
MODEL_SIZE := 5059027136

COMPOSE := docker compose

# Judges: set LLM_BASE_URL, LLM_API_KEY and LLM_NAME (or put them in .env), then `make run`.
# Prints the recorded evaluation (re-scored now), explains three letters live, and serves the
# UI at http://localhost:8000. Without an endpoint the UI replays recorded replies.
run:
	$(COMPOSE) build app
	$(COMPOSE) run --rm --service-ports app run

# On-premise / air-gapped: Apertus runs locally in llama.cpp, the model server has no internet.
onprem: model
	$(COMPOSE) --profile onprem build app-onprem
	$(COMPOSE) --profile onprem run --rm --service-ports app-onprem run

# Build-time download of the model file (about 5 GB), checked by size.
model: models/$(MODEL_FILE)
models/$(MODEL_FILE):
	mkdir -p models
	curl -L --fail --retry 5 -o $@.part \
	  "https://huggingface.co/$(MODEL_REPO)/resolve/$(MODEL_REV)/$(MODEL_FILE)"
	test "$$(wc -c < $@.part | tr -d ' ')" = "$(MODEL_SIZE)" && mv $@.part $@

# Re-run the evaluation against the configured endpoint (writes data/results/<model>.jsonl).
eval:
	$(COMPOSE) build app
	$(COMPOSE) run --rm app eval --langs en,uk

# Re-score every recorded reply offline; no model needed.
score:
	$(COMPOSE) build app
	$(COMPOSE) run --rm app score

test:
	$(COMPOSE) build app
	$(COMPOSE) run --rm --entrypoint python app -m unittest discover -s tests -v

clean:
	$(COMPOSE) --profile onprem down --remove-orphans
