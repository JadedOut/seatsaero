"""Shared route loading logic."""


def load_routes(path: str) -> list[tuple[str, str]]:
    """Load routes from file. Each line: 'ORIG DEST'. Skip blanks and # comments."""
    routes = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                routes.append((parts[0].upper(), parts[1].upper()))
    return routes
