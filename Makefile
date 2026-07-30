Q ?= kpop songs

.PHONY: update csv search

update:
	pip install -U -r requirements.txt

csv:
	python run.py csv $(if $(LIMIT),--limit $(LIMIT),)

search:
	python run.py search --q "$(Q)" $(if $(LIMIT),--limit $(LIMIT),)

