.PHONY: test install

test:
	python3 -m unittest discover -p 'test_*.py' -v

install:
	mkdir -p "$$HOME/bin"
	cp recall.py "$$HOME/bin/recall"
	chmod +x "$$HOME/bin/recall"
	@echo "installed -> $$HOME/bin/recall"
	@case ":$$PATH:" in \
	  *":$$HOME/bin:"*) ;; \
	  *) printf '\n⚠  %s is not on your PATH.\n   Add this to your shell rc (~/.zshrc) and restart the shell:\n     export PATH="$$HOME/bin:$$PATH"\n' "$$HOME/bin" ;; \
	esac
