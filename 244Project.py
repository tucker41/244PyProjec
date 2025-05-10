import tkinter as tk
from tkinter import ttk, messagebox
import openai
import os
from amadeus import Client, ResponseError
from dateutil import parser
import json
import tkinter.font as tkFont
import airportsdata

openai.api_key = os.getenv("OPENAI_API_KEY", "your open api key")
amadeus = Client(
    client_id=os.getenv("AMADEUS_CLIENT_ID", "your amadeus client id"),
    client_secret=os.getenv("AMADEUS_CLIENT_SECRET", "your amadeus client secret")
)
airports_db = airportsdata.load("IATA")  # Dictionary keyed by uppercase IATA codes
iata_cache = {}                          # For caching lookups

def fix_city_case(city_name):
    """
    If city_name minus spaces is all uppercase, convert to .title().
    E.g. "CHICAGO" -> "Chicago", "SAN DIEGO" -> "San Diego"
    """
    stripped = city_name.replace(" ", "")
    if stripped.isupper():
        return city_name.title()
    return city_name

def iata_to_city_name(iata_code):
    """
    Convert an IATA code (e.g. 'SAN') to a city name (e.g. 'San Diego')
    using the airportsdata library. Caches results to avoid repeated lookups.
    If not found, fallback to the code itself.
    """
    code_upper = iata_code.upper()
    if code_upper in iata_cache:
        return iata_cache[code_upper]

    record = airports_db.get(code_upper)
    if record:
        # Usually record has: { 'iata': 'SAN', 'city': 'San Diego', 'name': ... }
        city = record.get('city') or record.get('name', code_upper)
    else:
        city = code_upper

    final_city = fix_city_case(city)
    iata_cache[code_upper] = final_city
    return final_city

def ask_openai(prompt):
    """
    Send a prompt to GPT and return its response text.
    """
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o", 
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful AI travel assistant. Flights come from Amadeus real-time data. "
                        "Hotels are recommended based on user city and budget. Provide short, creative suggestions."
                    )
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=700,
            temperature=0.7,
        )
        return response["choices"][0]["message"]["content"]
    except Exception as e:
        return f"An error occurred while calling OpenAI API: {e}"

def get_iata_code(city_query):
    """
    Convert a city/airport name (e.g. 'Chicago') to an IATA code (e.g. 'ORD').
    """
    try:
        response = amadeus.reference_data.locations.get(
            keyword=city_query,
            subType='AIRPORT,CITY',
        )
        data = response.data
        if not data:
            return None
        return data[0]['iataCode']  
    except ResponseError as err:
        print(f"Error in get_iata_code: {err}")
        return None

def get_flights(origin_iata, destination_iata, departure_date, return_date, adults=1):
    """
    Fetch exactly ONE flight offer from Amadeus.
    """
    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=origin_iata,
            destinationLocationCode=destination_iata,
            departureDate=departure_date,
            returnDate=return_date,
            adults=adults,
            currencyCode='USD',
            max=1
        )
        return response.data
    except ResponseError as err:
        print(f"Error in get_flights: {err}")
        return []

def parse_single_flight_offer(flight_offers):
    """
    Nicely format flight data: price, segments, etc.
    Now uses iata_to_city_name(...) to produce "Chicago (ORD)" etc.
    """
    if not flight_offers:
        return "No flight offers found for these dates."

    offer = flight_offers[0]
    total_price = offer['price']['total']

    summary_lines = []
    summary_lines.append("Flight detail")
    summary_lines.append(f"Total Price (per adult): ${total_price}\n")

    itineraries = offer.get('itineraries', [])
    segment_counter = 1

    for itinerary in itineraries:
        segments = itinerary.get('segments', [])
        for seg in segments:
            dep_code = seg['departure']['iataCode']  
            dep_time_str = seg['departure']['at']
            arr_code = seg['arrival']['iataCode']
            arr_time_str = seg['arrival']['at']

            dep_dt = parser.parse(dep_time_str)
            dep_date = dep_dt.strftime("%B %d, %Y")
            dep_time = dep_dt.strftime("%I:%M %p")

            arr_dt = parser.parse(arr_time_str)
            arr_date = arr_dt.strftime("%B %d, %Y")
            arr_time = arr_dt.strftime("%I:%M %p")

            # Convert IATA->City using airportsdata
            dep_city_name = iata_to_city_name(dep_code)
            arr_city_name = iata_to_city_name(arr_code)

            summary_lines.append(f"Segment {segment_counter}:")
            summary_lines.append(f"  From: {dep_city_name} ({dep_code})")
            summary_lines.append(f"  Date: {dep_date}")
            summary_lines.append(f"  Time: {dep_time}\n")

            summary_lines.append(f"  To: {arr_city_name} ({arr_code})")
            summary_lines.append(f"  Date: {arr_date}")
            summary_lines.append(f"  Time: {arr_time}\n")

            segment_counter += 1

    return "\n".join(summary_lines)

def get_gpt_hotels(destination, budget_per_night):
    """
    Asks GPT for 5 real hotels in 'destination' that typically cost < budget_per_night.
    """
    user_prompt = (
        f"Recommend exactly 5 real hotels in {destination} that typically have an average "
        f"nightly rate at or under ${budget_per_night:.2f} USD. "
        "Only provide valid JSON—no code fences or extra text. The response must look like:\n"
        "[\n"
        "  {\n"
        '    \"name\": \"Hotel Name\",\n'
        '    \"price\": \"Approx. 120\",\n'
        '    \"address\": \"123 Street, City\"\n'
        "  },\n"
        "  ... (5 total) ...\n"
        "]"
    )

    response = ask_openai(user_prompt)
    response_stripped = response.strip().strip("```").strip()
    try:
        hotel_list = json.loads(response_stripped)
        if isinstance(hotel_list, list) and len(hotel_list) == 5:
            return hotel_list
        else:
            print("GPT did not return a list of exactly 5 hotels.")
            return []
    except Exception as e:
        print("Failed to parse GPT hotel data as JSON:", e)
        print("GPT Response was:", response)
        return []

def generate_hotel_description(hotel_name, city, address, approx_price):
    """
    GPT short description for each hotel.
    """
    prompt = (
        f"Write a short, engaging description for a hotel named '{hotel_name}' located in {city}. "
        f"The approximate nightly rate is {approx_price}. Its address is: {address}. "
        "Highlight proximity to popular landmarks or city centers, and the general vibe."
    )
    return ask_openai(prompt)

def parse_gpt_hotels(hotels):
    """
    For each GPT-provided hotel, generate a short description. Return combined text.
    """
    summaries = []
    for i, h in enumerate(hotels, start=1):
        name = h.get("name", "Unknown Hotel")
        price = h.get("price", "N/A")
        address = h.get("address", "N/A")

        city_guess = "the destination"
        description = generate_hotel_description(name, city_guess, address, price)

        summary = (
            f"Hotel Option #{i}\n"
            f"Name: {name}\n"
            f"Approx. Price/Night: {price}\n"
            f"Address: {address}\n"
            f"Description: {description}\n"
        )
        summaries.append(summary)
    return summaries

class TravelPlannerGUI:
    def __init__(self, root):
        self.root = root

        # Larger default window
        self.root.geometry("900x700")
        self.root.resizable(True, True)
        self.root.title("AI-Assisted Travel Planner")

        # Larger default font
        default_font = tkFont.Font(family="Arial", size=12)
        self.root.option_add("*Font", default_font)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Modern.TButton",
            background="#3496f0",
            foreground="white",
            padding=(10, 6),
            relief="flat"
        )
        style.map(
            "Modern.TButton",
            background=[("active", "#1e7fcc"), ("pressed", "#1666a8")],
            relief=[("pressed", "groove")]
        )

        self.departure_var = tk.StringVar()
        self.destination_var = tk.StringVar()
        self.start_date_var = tk.StringVar()
        self.end_date_var = tk.StringVar()
        self.budget_var = tk.StringVar()
        self.selected_hotel = tk.StringVar()
        self.user_interests = tk.StringVar()
        self.food_interests = tk.StringVar()

        # Data storage
        self.single_flight_info = ""
        self.hotel_offers = []
        self.hotel_summaries = ""
        self.activities_info = ""
        self.final_summary = ""

        # Main container
        self.main_container = ttk.Frame(self.root, padding="10 10 10 10")
        self.main_container.grid(row=0, column=0, sticky="nsew")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.create_input_frame()

    def create_input_frame(self):
        """
        First screen: departure/destination, dates, budget
        """
        self.input_frame = ttk.Frame(self.main_container, padding="10 10 10 10")
        self.input_frame.grid(row=0, column=0, sticky="nsew")

        self.main_container.rowconfigure(0, weight=1)
        self.main_container.columnconfigure(0, weight=1)

        # Rows
        ttk.Label(self.input_frame, text="Departing from (City or Airport):").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.input_frame, textvariable=self.departure_var, width=40).grid(row=0, column=1, sticky="ew", padx=5, pady=5)

        ttk.Label(self.input_frame, text="Where would you like to travel?").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.input_frame, textvariable=self.destination_var, width=40).grid(row=1, column=1, sticky="ew", padx=5, pady=5)

        ttk.Label(self.input_frame, text="Start date (YYYY-MM-DD):").grid(row=2, column=0, sticky="w")
        ttk.Entry(self.input_frame, textvariable=self.start_date_var, width=40).grid(row=2, column=1, sticky="ew", padx=5, pady=5)

        ttk.Label(self.input_frame, text="End date (YYYY-MM-DD):").grid(row=3, column=0, sticky="w")
        ttk.Entry(self.input_frame, textvariable=self.end_date_var, width=40).grid(row=3, column=1, sticky="ew", padx=5, pady=5)

        ttk.Label(self.input_frame, text="Hotel budget (per night):").grid(row=4, column=0, sticky="w")
        ttk.Entry(self.input_frame, textvariable=self.budget_var, width=40).grid(row=4, column=1, sticky="ew", padx=5, pady=5)

        submit_button = ttk.Button(
            self.input_frame,
            text="Submit",
            command=self.handle_travel_info,
            style="Modern.TButton"
        )
        submit_button.grid(row=5, column=0, columnspan=2, pady=10)

        self.input_frame.columnconfigure(1, weight=1)

    def handle_travel_info(self):
        """
        Grab user inputs, convert city->IATA, fetch flight, GPT hotels, show next frame.
        """
        departure_city = self.departure_var.get().strip()
        destination_city = self.destination_var.get().strip()
        start_date = self.start_date_var.get().strip()
        end_date = self.end_date_var.get().strip()

        budget_str = self.budget_var.get().strip()
        if not budget_str:
            budget_per_night = 999999.0
        else:
            try:
                budget_per_night = float(budget_str)
            except ValueError:
                messagebox.showerror("Error", "Hotel budget must be a number.")
                return

        if not departure_city or not destination_city or not start_date or not end_date:
            messagebox.showerror("Error", "Please fill in departure, destination, and start/end dates.")
            return

        origin_iata = get_iata_code(departure_city)
        dest_iata = get_iata_code(destination_city)
        if not origin_iata:
            messagebox.showerror("Error", f"Could not find IATA code for departure: {departure_city}")
            return
        if not dest_iata:
            messagebox.showerror("Error", f"Could not find IATA code for destination: {destination_city}")
            return

        # 1) Flight
        flight_data = get_flights(origin_iata, dest_iata, start_date, end_date)
        self.single_flight_info = parse_single_flight_offer(flight_data)

        # 2) GPT hotels
        hotels = get_gpt_hotels(destination_city, budget_per_night)
        self.hotel_offers = hotels
        if not hotels:
            self.hotel_summaries = "No hotels found by GPT or an error occurred."
        else:
            parsed_hotels = parse_gpt_hotels(hotels)
            self.hotel_summaries = "\n".join(parsed_hotels)

        self.input_frame.destroy()
        self.create_flight_hotel_frame()

    def create_flight_hotel_frame(self):
        """
        Show flight info + GPT-based hotel list
        """
        self.flight_hotel_frame = ttk.Frame(self.main_container, padding="10 10 10 10")
        self.flight_hotel_frame.grid(row=0, column=0, sticky="nsew")

        self.main_container.rowconfigure(0, weight=1)
        self.main_container.columnconfigure(0, weight=1)

        ttk.Label(self.flight_hotel_frame, text="Flight Info:").grid(row=0, column=0, sticky="w")

        # Flight info with scrollbar
        flight_text_frame = ttk.Frame(self.flight_hotel_frame)
        flight_text_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=5)

        self.flight_text_box = tk.Text(flight_text_frame, wrap="word")
        self.flight_text_box.grid(row=0, column=0, sticky="nsew")
        self.flight_text_box.insert("1.0", self.single_flight_info)
        self.flight_text_box.config(state="disabled")

        flight_scrollbar = ttk.Scrollbar(flight_text_frame, orient="vertical", command=self.flight_text_box.yview)
        flight_scrollbar.grid(row=0, column=1, sticky="ns")
        self.flight_text_box["yscrollcommand"] = flight_scrollbar.set

        flight_text_frame.rowconfigure(0, weight=1)
        flight_text_frame.columnconfigure(0, weight=1)

        ttk.Label(self.flight_hotel_frame, text="Hotel Options:").grid(row=2, column=0, sticky="w")

        hotel_text_frame = ttk.Frame(self.flight_hotel_frame)
        hotel_text_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=5)

        self.hotel_box = tk.Text(hotel_text_frame, wrap="word")
        self.hotel_box.grid(row=0, column=0, sticky="nsew")
        self.hotel_box.insert("1.0", self.hotel_summaries)
        self.hotel_box.config(state="disabled")

        hotel_scrollbar = ttk.Scrollbar(hotel_text_frame, orient="vertical", command=self.hotel_box.yview)
        hotel_scrollbar.grid(row=0, column=1, sticky="ns")
        self.hotel_box["yscrollcommand"] = hotel_scrollbar.set

        hotel_text_frame.rowconfigure(0, weight=1)
        hotel_text_frame.columnconfigure(0, weight=1)

        ttk.Label(self.flight_hotel_frame, text="Enter the hotel name or # you choose:").grid(row=4, column=0, sticky="w")
        ttk.Entry(self.flight_hotel_frame, textvariable=self.selected_hotel, width=40).grid(row=4, column=1, sticky="ew", padx=5, pady=5)

        next_button = ttk.Button(
            self.flight_hotel_frame,
            text="Next",
            command=self.handle_hotel_choice,
            style="Modern.TButton"
        )
        next_button.grid(row=5, column=0, columnspan=2, pady=10)

        self.flight_hotel_frame.rowconfigure(1, weight=1)
        self.flight_hotel_frame.rowconfigure(3, weight=1)
        self.flight_hotel_frame.columnconfigure(0, weight=1)
        self.flight_hotel_frame.columnconfigure(1, weight=1)

    def handle_hotel_choice(self):
        chosen = self.selected_hotel.get().strip()
        if not chosen:
            messagebox.showerror("Error", "Please enter the hotel you wish to choose.")
            return
        self.flight_hotel_frame.destroy()
        self.create_interests_frame()

    def create_interests_frame(self):
        """
        Ask about user interests/food, then GPT for day-by-day itinerary
        """
        self.interests_frame = ttk.Frame(self.main_container, padding="10 10 10 10")
        self.interests_frame.grid(row=0, column=0, sticky="nsew")

        self.main_container.rowconfigure(0, weight=1)
        self.main_container.columnconfigure(0, weight=1)

        ttk.Label(self.interests_frame, text="What are your interests? (e.g., museums, hiking)").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.interests_frame, textvariable=self.user_interests, width=40).grid(row=0, column=1, sticky="ew", padx=5, pady=5)

        ttk.Label(self.interests_frame, text="What kind of food do you like? (e.g., seafood, vegetarian)").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.interests_frame, textvariable=self.food_interests, width=40).grid(row=1, column=1, sticky="ew", padx=5, pady=5)

        next_button = ttk.Button(
            self.interests_frame,
            text="Show Activities",
            command=self.get_activities,
            style="Modern.TButton"
        )
        next_button.grid(row=2, column=0, columnspan=2, pady=10)

        self.interests_frame.columnconfigure(1, weight=1)

    def get_activities(self):
        """
        Ask GPT for a day-by-day itinerary with at least 1 activity & restaurant per day
        """
        interests = self.user_interests.get().strip()
        food = self.food_interests.get().strip()
        if not interests or not food:
            messagebox.showerror("Error", "Please enter your interests and food preferences.")
            return

        start_date_str = self.start_date_var.get().strip()
        end_date_str = self.end_date_var.get().strip()
        destination_city = self.destination_var.get().strip()
        chosen_hotel = self.selected_hotel.get().strip()

        try:
            start_dt = parser.parse(start_date_str)
            end_dt = parser.parse(end_date_str)
            total_days = (end_dt - start_dt).days + 1
            if total_days < 1:
                messagebox.showerror("Error", "Your end date must be after your start date.")
                return
        except Exception as e:
            messagebox.showerror("Error", f"Could not parse dates: {e}")
            return

        prompt = (
            f"I am traveling to {destination_city} from {start_date_str} to {end_date_str} "
            f"(a total of {total_days} days) and staying at '{chosen_hotel}'. "
            f"I enjoy {interests} and prefer {food} cuisine. "
            "Please create a day-by-day itinerary, providing at least one recommended activity "
            "and one recommended restaurant for each day. Label each day as 'Day 1', 'Day 2', etc. "
            "Focus on attractions and food options that match my preferences."
        )

        self.activities_info = ask_openai(prompt)

        self.interests_frame.destroy()
        self.create_final_summary_frame()

    def create_final_summary_frame(self):
        """
        Combine flight info, hotels, and daily itinerary into a final summary.
        """
        self.summary_frame = ttk.Frame(self.main_container, padding="10 10 10 10")
        self.summary_frame.grid(row=0, column=0, sticky="nsew")

        self.main_container.rowconfigure(0, weight=1)
        self.main_container.columnconfigure(0, weight=1)

        departure_city = self.departure_var.get()
        destination_city = self.destination_var.get()
        chosen_hotel = self.selected_hotel.get().strip()

        summary_prompt = (
            "Create a concise final travel summary that includes:\n\n"
            f"1) The single flight option from {departure_city} to {destination_city}:\n{self.single_flight_info}\n\n"
            f"2) The five GPT-recommended hotels under the user's budget, and the chosen hotel: {chosen_hotel}.\n"
            f"Hotel options:\n{self.hotel_summaries}\n\n"
            f"3) Day-by-day activities & restaurants recommended:\n{self.activities_info}\n\n"
            "Please format it nicely for the user, but do not ask for more data."
        )

        self.final_summary = ask_openai(summary_prompt)

        ttk.Label(self.summary_frame, text="Your Final Trip Summary:").grid(row=0, column=0, sticky="w")

        # Add a scrollable text box
        summary_text_frame = ttk.Frame(self.summary_frame)
        summary_text_frame.grid(row=1, column=0, sticky="nsew", pady=5)

        text_box = tk.Text(summary_text_frame, wrap="word")
        text_box.grid(row=0, column=0, sticky="nsew")
        text_box.insert("1.0", self.final_summary)
        text_box.config(state="disabled")

        summary_scrollbar = ttk.Scrollbar(summary_text_frame, orient="vertical", command=text_box.yview)
        summary_scrollbar.grid(row=0, column=1, sticky="ns")
        text_box["yscrollcommand"] = summary_scrollbar.set

        summary_text_frame.rowconfigure(0, weight=1)
        summary_text_frame.columnconfigure(0, weight=1)

        ttk.Label(self.summary_frame, text="End of AI-Assisted Travel Planning").grid(row=2, column=0, pady=10, sticky="nsew")

        self.summary_frame.rowconfigure(1, weight=1)
        self.summary_frame.columnconfigure(0, weight=1)

def main():
    root = tk.Tk()
    app = TravelPlannerGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()