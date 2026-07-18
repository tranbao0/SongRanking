Q ?= kpop songs

.PHONY: update csv search

update:
	pip install -U -r requirements.txt

csv:
	python src/pipeline.py $(if $(LIMIT),--limit $(LIMIT),)

search:
	python src/pipeline.py --search "$(Q)" $(if $(LIMIT),--limit $(LIMIT),)

