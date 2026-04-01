5. Operations: The "Meeting Intelligence" Hub
1. Background
Every organization holds dozens of internal and client meetings every week. These
meetings produce hours of audio and video content each day. Today, tools exist that can
convert speech into text transcripts. However, a single one-hour meeting can easily
produce a transcript that is 20 or more pages long.
The problem is not generating transcripts. The real problem is that nobody has the time to
read through these long documents to find the specific piece of information they need.
Stakeholders need to quickly locate key decisions that were made, action items that were
assigned, and the reasoning behind important strategies. When this information is buried in
pages of dialogue, it effectively gets lost.
This creates a painful cycle that we call "Double Work." Instead of executing the tasks
discussed in a meeting, team members spend significant time asking each other, "What
happened in that meeting?" or "Did we decide to go ahead with that approach?" This
wastes time, creates confusion, and slows down the entire team.
3. Detailed Feature Requirements
Feature 1: Multi-Transcript Ingestion Portal
Build an upload portal where users can upload one or more meeting transcripts. The
system should support common transcript formats such as plain text (.txt) and WebVTT
(.vtt) subtitle files.
•A user should be able to drag-and-drop or browse to select multiple files at once.
•Uploaded transcripts should be stored and organized so the system can reference
them later (for example, grouped by project name or meeting date).
•The portal should validate the file type and show a clear error message if an
unsupported format is uploaded.
•After upload, display a summary of each transcript (for example, file name, detected
meeting date, number of speakers identified, and total word count).
Feature 2: Decision and Action Item Extractor
Build a feature that automatically reads through each uploaded transcript and identifies
key decisions and action items. For every action item found, the system should extract and
display three things: Who is responsible, What they need to do, and By When it should be
completed.•Parse the dialogue and extract structured information.
•Present the extracted data in a clean, readable table format on the front end.
•Differentiate between "Decisions" (things the team agreed on) and "Action Items"
(tasks assigned to specific people).
•Provide an option to export the decisions and action items as a downloadable file
(CSV or PDF).
Feature 3: Contextual Query Engine (Chatbot)
Build a chatbot-style interface that lets users ask natural language questions across all
uploaded meeting transcripts. The chatbot should be able to answer complex, cross-
meeting questions by searching through and reasoning over the content of multiple
transcripts at once.
•The chatbot should understand context. For example, if a user asks, "Why did we
decide to delay the API launch?" the system should find the relevant discussion
across one or more transcripts and provide a summarized answer with references.
•It should also handle questions like, "What were the three main concerns raised by
the Finance Lead?" by identifying the correct speaker and their points.
•The chatbot should cite its sources, meaning it should tell the user which meeting
and which part of the transcript the answer came from.
Feature 4: Speaker Sentiment and Tone Analysis
Build a visual dashboard that analyses the overall "vibe" of each meeting. This feature
should identify areas of high conflict, strong agreement, frustration, or enthusiasm in the
dialogue and present them in a way that is easy to understand at a glance.
•Perform sentiment analysis on the transcript, ideally at the speaker level and at the
segment level (for example, every 5 minutes of dialogue).
•Display results using visual indicators such as color-coded timelines, bar charts, or
emoji-based markers (for example, green for consensus, red for conflict, yellow for
uncertainty).
•Allow users to click on a flagged section to view the original transcript text for that
segment.•
Show a per-speaker sentiment breakdown so managers can understand which team
members were aligned and which had concerns.
4. Minimum end-to-end expectations
1. Dashboard Home Page: A landing page that shows a list of uploaded
projects/meetings with quick stats (number of transcripts, total action items, overall
sentiment score).
2. Upload Interface: A clean drag-and-drop area or file browser for uploading
transcripts, with upload progress and file validation feedback.
3. Meeting Detail View: When a user clicks on a meeting, they should see the
extracted decisions and action items in a structured table, the sentiment analysis
visualization, and the chatbot interface for that meeting context.
4. Chatbot Panel: A chat window (like a messaging app) where users can type
questions and receive AI-generated answers with citations.