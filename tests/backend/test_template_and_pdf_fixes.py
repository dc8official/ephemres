import base64
import pytest
from app.core.template import Template
from app.core.pdf import HTML

# Minimal valid 1x1 RGB PNG
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)

def test_template_blocks_dunder_attribute_access():
    tmpl = Template("{{ user.__class__.__name__ }}")
    rendered = tmpl.render(user="alice")
    assert rendered == ""

def test_pdf_embeds_logo_xobject_and_preserves_long_cells_and_footer():
    html_str = f"""
    <html>
      <head>
        <style>@page {{ @bottom-left {{ content: "Acme &amp; Co &#x27;2026&#x27;"; }} }}</style>
      </head>
      <body>
        <div class="doc-header">
          <img src="data:image/png;base64,{TINY_PNG_B64}" class="org-logo" />
          <h1>Acme Telecom</h1>
        </div>
        <table>
          <thead>
            <tr><th>Node</th><th>IP</th><th>Start</th><th>End</th><th>Dur</th><th>State</th><th>Loss</th><th>Jitter</th><th>RCA Summary</th></tr>
          </thead>
          <tbody>
            <tr>
              <td>core-router-lon-01</td>
              <td>192.0.2.254</td>
              <td>2026-10-17 11:00:00</td>
              <td>2026-10-17 11:15:00</td>
              <td>900s</td>
              <td>DOWN</td>
              <td>100%</td>
              <td>0ms</td>
              <td>Upstream transit hop 3 packet loss</td>
            </tr>
          </tbody>
        </table>
      </body>
    </html>
    """
    pdf_bytes = HTML(string=html_str).write_pdf()
    assert b"/Subtype /Image" in pdf_bytes
    assert b"/ImLogo Do" in pdf_bytes
    assert b"Acme & Co '2026'" in pdf_bytes
    assert b"core-router-lon-01" in pdf_bytes
    assert b"2026-10-17 11:00:00" in pdf_bytes
