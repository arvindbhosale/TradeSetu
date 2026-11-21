from .app import main

if __name__ == "__main__":       # worker processes (spawn) import this module again; they must not start the app
    main()
