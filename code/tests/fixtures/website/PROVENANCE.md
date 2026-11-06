These small fixtures retain the relevant shapes from public pages inspected on
2026-11-05 and 2026-11-06. Identifiers are shortened where the fixture uses example URLs.

- `drive_folder.html`, `drive_child.html`: Ranger ISD's public embedded Drive
  folder, including nested folders and date-bearing file names. Resource-key
  traversal follows the keyed Drive link published by Poolville ISD.
- `apptegy_bootstrap.html`: Conroe ISD's `window.clientWorkStateTemp` JSON.parse
  bootstrap, with collapsed Board Packets nested under a school-year heading.
- `apptegy_nodes.json`: Alto ISD's accordion panel properties and bold date
  paragraphs; it also covers month-only minutes labels observed at Wall ISD.
- `apptegy_columns.json`: Wall ISD's two-column Board Meeting Minutes block,
  reduced to names, node types, content and published links.
- `apptegy_pinia.json`: the indexed Nuxt/Pinia page shape used by Apptegy sites,
  with empty root data and the actual page under pinia.main.page.
- `schoolblocks_flight.html`: Axtell ISD's Next.js hydration file metadata,
  retaining the published meeting date separately from modifiedTimestamp.
- `cms_hosts.html`: document hosts observed on Robert Lee, La Gloria, Blanket,
  Alvord and Pringle-Morse district pages. The unrelated host is a negative
  control and must never be collected.
- `granicus.html`, `granicus_download.json`: El Paso ISD's ViewPublisher rows
  and AgendaViewer redirect to the underscore-named public S3 bucket.
- `swagit.html`: Crowley ISD's public video archive row and agenda route.
- `socs_layout.html`: Charlotte ISD's layout table containing both policy
  navigation and dated board agenda links.

Existing fixtures predate this repair. Browser regressions use these saved
HTML shapes and mock the browser transport, so tests never open live sessions.

- `php_board.html`: Patton Springs PHP content-route navigation and true numeric pagination.
- `drive_iframe.html`: the Google Drive embedding shape published by Slocum and Smyer;
  these public folder URLs do not require a browser render.
- `apptegy_navigation.json`: district folder navigation advertised by Apptegy,
  including a non-board assessment folder that board context must not admit.

- `drive_minutes.docx`: a minimal Word document for the observed Google
  usercontent application/octet-stream + Content-Disposition DOCX response;
  its regression runs the real shared text extractor after adapter resolution.

- `paperless_meetings.html`, `paperless_agenda.html`: Charlotte ISD public
  Paperless Board Meetings index and HTML agenda; an anonymous guest session
  is initialized at the index before requesting agenda bodies.

- `novus_agenda.html`, `novus_unavailable.html`: El Paso Granicus AgendaViewer
  redirects to NovusAGENDA MeetingView HTML, with form/error markers and
  public CoverSheet links; valid HTML must not be rejected as missing PDF.

- `download_challenge.html`: Client Challenge interstitial observed at an old
  Conroe wp-content PDF URL; download retry must refresh cached bytes after
  browser clearance and scope transferred cookies to the source host.

Native-document alias regression uses the PDF response contract: Coolidge
2018 Google Docs shared as Drive file links return 5xx from usercontent, while
the same IDs export public PDF successfully through docs.google.com.
