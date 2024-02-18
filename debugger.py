import cli

if __name__ == "__main__":
    try:
        from dotenv import dotenv_values

        env = dotenv_values(".env")
        print(f"loaded from .env {env=}")
        githubpat = env["github_pat"]
    except ImportError:
        print("install requirements-dev.txt into your venv")
        raise
    except KeyError:
        print("update .env with your github pat and assign to key: github_pat")
        raise
    args = [
        "--token", githubpat,
        "--repository", "clowtown/pr-enforcer",
        "--branch", "test",  # TODO update with your branch
        "--interval", "10",
        "--timeout", "300",
        "--name", "enforce-all-checks",
        "--ignore", "label, CodeQL, bridgecrew",
        "--exhaustive",
        "--debug"
    ]
    cli.hello(args)
