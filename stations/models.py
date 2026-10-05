from django.db import models


class Station(models.Model):

    PRECISION_ADDRESS = 'address'
    PRECISION_CITY = 'city'
    PRECISION_CITY_FUZZY = 'city_fuzzy'
    PRECISION_CITY_GEOCODED = 'city_geocoded'

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    price = models.DecimalField(max_digits=7, decimal_places=4)
    lat = models.FloatField()
    lon = models.FloatField()
    geo_precision = models.CharField(max_length=20, default=PRECISION_CITY)
    popularity = models.FloatField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['lat', 'lon'])]

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state}) ${self.price}'
