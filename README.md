# Underground Ballot
This program is intended to read and write a Google Sheet with a recommended ordering of candidates,
then create PDF ballots with a rendering of the recommended ordering.

This is intended to be used in conjunction with
[candidate-scraping](https://github.com/Underground-Foundation/candidate-scraping).

## Setup
1. Install dependencies (ideally in a virtualenv): `pip install -r requirements.txt`
2. Put a Google service-account key file in the project directory (or anywhere) and point to it in `.env`:
   ```
   DEFAULT_GOOGLE_KEY="underground-candidates-xxxx.json"
   ```
3. Share the spreadsheet with the service account's email (as Editor), eg <shell-access@underground-candidates.iam.gserviceaccount.com>

## Usage
### Scoring Sheet
A Google Sheet needs to be initialised with all of the candidates, ready for you to score them. Once you've downloaded
a CSV with all the candidates (using eg 
[candidate-scraping](https://github.com/Underground-Foundation/candidate-scraping)), then use it to initialise the
sheet:
```
python upsert_candidates.py [--url '<spreadsheet url>'] [--input candidates.csv]
```
This will create a sheet "All candidate scores".

To give every candidate of a party the same recommendation in the sheet (matching the party name, ignoring case):
```
python apply_recommendations_remotely.py [--url '<spreadsheet url>'] --party Fusion --recommendation G
```

Once you have made scores / recommendations about each row, download your finished sheet (the default filename is 
`candidates_with_scores.csv`:
```
python download_candidate_scores.py [--url '<spreadsheet url>'] [--output candidates_with_scores.csv]
```

### Rendering 
Before rendering, we must first discover the font etc to be used, by relying on an example PDF:
```
python pdf_inspection.py Footscray.pdf --candidate-text 'HALL, Katie' > rendering.json
```

With these details, we can then render voting-guide PDFs for each of our electorates from <candidates_with_scores.csv>:

```
python render_recommendations.py --rendering rendering.json [--base-pdf base.pdf] [--input candidates_with_scores.csv] [--output-dir ballots]
```
Each electorate's candidates are drawn onto a copy of the base PDF (a blank ballot) and saved as
`ballots/<Electorate>.pdf`. Only electorates with a `Recommendation` for at least one candidate are rendered.

Each party's logo is the file in `party_logos/` named exactly after the party (ignoring case and file type), or else
the only file whose name contains the party's name as whole words (ignoring anything in parentheses). Independents get no logo, and a party without a unique match gets
none, with a warning; to fix that, add a logo named exactly after the party.
