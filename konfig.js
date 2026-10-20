// Oppsett for den vanlige siden.
window.ASFALTKART_KONFIG = {
  // Delte markeringer. Adressen finner du i Supabase under Project Settings, «Data API» (Project URL).
  supabaseUrl: 'https://ycwrgdngljmjxvsadquj.supabase.co',
  supabaseKey: 'sb_publishable_DedXgUhb8ihxvery1Jw3mg_-mnLzcXf',

  // Flyfoto (valgfritt). Sett aerial: null for å droppe det.
  // Mapbox: lag gratis konto på mapbox.com, hent access token, og lim den inn der det står DIN_TOKEN.
  aerial: {
    label: 'Flyfoto',
    url: 'https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}@2x.jpg90?access_token=pk.eyJ1Ijoicm9zYWdhcmRpbmVyIiwiYSI6ImNtdmd1Z2hmYTA1bG8yeHB1MWg4Z2k0OHAifQ.tHu1iFuPRD53OanqZ-6pNQ',
    attribution: '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> &copy; Maxar',
    maxZoom: 19
  }
};
