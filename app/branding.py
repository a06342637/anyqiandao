from html import escape


def public_branding(store):
    settings = store.settings()
    return {'site_name': settings.site_name, 'site_icon_text': settings.site_icon_text}


def favicon_svg(text):
    size = 36 if len(text) == 1 else 25
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="17" fill="#c4432e"/>'
            '<rect x="2" y="2" width="60" height="60" rx="15" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2"/>'
            f'<text x="32" y="34" text-anchor="middle" dominant-baseline="central" fill="#fff" '
            f'font-family="Songti SC,STSong,SimSun,serif" font-size="{size}" font-weight="700">{escape(text)}</text></svg>')
