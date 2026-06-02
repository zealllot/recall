.PHONY: test install

test:
	python3 -m unittest discover -p 'test_*.py' -v

install:
	cp recall.py $(HOME)/bin/recall
	chmod +x $(HOME)/bin/recall
	@echo "installed -> $(HOME)/bin/recall"
