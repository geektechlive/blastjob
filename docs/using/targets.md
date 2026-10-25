# Target companies

**What this covers:** How to keep a list of companies you want to work for, what the weekly news scan finds for them, how to read an item, and what a failed scan looks like. It is for anyone using the cockpit. For how the scan works inside, see [employer data](../how-it-works/employer-data.md#target-company-news-watch).

## Why a list

Jobs at a senior level are often filled before they are posted, and a leadership hire often follows news: a new chief executive, an acquisition, a private-equity buyer, a large ERP or cloud program. The target list tells blastjob which companies to watch for that kind of news, and shows it next to the people you already know there, so you can reach out early.

## Add companies

Open **Targets**, under **More** then **Research**. In **Add target companies**, put one company per line and press **Add**:

```
Acme Widgets | acme.example.com
Northwind Traders
Contoso Foods | | WI
```

- After a `|`, you can add the company's own website domain. It must be a plain host name such as `acme.example.com`, with no `https://` and no path. Job boards and shared hosting domains are refused.
- A second `|` adds a two-letter state, for a company outside your home state. Without it, your home state from **Settings** is used.
- A domain is optional but worth adding. News found on a domain you entered is labelled as coming from the employer's own site. Without one, every item is labelled a news report.
- A line that cannot be read is listed under the form with the reason. It is not added, and the other lines still are.
- Adding the same company twice does nothing. "Acme Widgets, Inc." and "ACME WIDGETS" are the same company.

Choose the list in the **List** menu. Core companies are scanned every week. Dream is a place to park companies for later; they are stored but not scanned for news yet. Each row has a button to move a company between the two, and **Remove**. Removing a company hides its news, and adding it back shows that news again.

## What the weekly scan finds

Once a week (inside the daily run, at most 8 companies at a time), blastjob searches for news about each core company and keeps only the kinds that often come before a hire: a new chief executive, financial or technology officer, an IT leader leaving, an acquisition, a private-equity deal, a merger, funding, an ERP or cloud program, or a restructuring.

An item is kept only when:

- the page is about that company (its own domain, or its name with your state close by);
- the page states a publication date, within the last 120 days;
- the sentence shown is copied word for word from that page and says the event happened. Plans that were only considered, rumored, denied or dropped are left out.

## Reading an item

Each item shows the kind of news, "published" and a date, where it came from, a sentence and a link:

- The date is when the page was published, not when the event happened. An old event can appear on a recent page.
- "employer's own site" means the page is on a domain you entered or configured for that company. "news report" means any other site that passed the checks. Neither is a confirmation. Read the page before you act on it.
- The sentence is the page's own, never a summary written by the model.
- **Dismiss** hides an item you have dealt with or do not want to see.

News never goes into a resume, cover letter or prep pack. It is for you to read and use in your own words.

## People you know

Each row shows how many contacts you have there and how many are decision makers, from the LinkedIn export you imported (see [your network](../how-it-works/network.md)). Click a company to open its page, which lists those people, with decision makers marked, and links to their profiles. The page works even for a company you have never researched. Verify a person before you write to them: it is your own export, not a lookup.

## When a scan fails

The **Last scan** column says **Scan failed** with a short reason when a search, the model step or the page reader did not work. A page that could not be read counts as a failure unless the scan still found news. That is not the same as no news. A failed scan keeps the date of the last good scan, and is tried again after a day, then three days, then seven. The digest says how many scans failed in the run. If you see "Not scanned yet" for every company, check that `JINA_API_KEY` is set (see [settings and secrets](../operations/settings-and-secrets.md)); without it nothing is scanned, and the digest notes that.

## In the digest

Items found since the last digest appear under "Target news", up to 8 at a time. See [the daily digest](digest.md).
